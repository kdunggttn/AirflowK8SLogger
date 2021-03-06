import logging
import os
from pathlib import Path
from typing import Optional

import requests

from airflow.configuration import AirflowConfigException, conf
from airflow.models import TaskInstance
from airflow.utils.helpers import parse_template_string


class FileTaskHandler(logging.Handler):
    """
    FileTaskHandler is a python log handler that handles and reads
    task instance logs. It creates and delegates log handling
    to `logging.FileHandler` after receiving task instance context.
    It reads logs from task instance's host machine.
    :param base_log_folder: Base log folder to place logs.
    :param filename_template: template filename string
    """
    def __init__(self, base_log_folder: str, filename_template: str):
        super().__init__()
        self.handler = None  # type: Optional[logging.FileHandler]
        self.local_base = base_log_folder
        self.filename_template, self.filename_jinja_template = \
            parse_template_string(filename_template)

    def set_context(self, ti: TaskInstance):
        """
        Provide task_instance context to airflow task handler.
        :param ti: task instance object
        """
        local_loc = self._init_file(ti)
        self.handler = logging.FileHandler(local_loc, encoding='utf-8')
        if self.formatter:
            self.handler.setFormatter(self.formatter)
        self.handler.setLevel(self.level)

    def emit(self, record):
        if self.handler:
            self.handler.emit(record)

    def flush(self):
        if self.handler:
            self.handler.flush()

    def close(self):
        if self.handler:
            self.handler.close()

    def _render_filename(self, ti, try_number):
        if self.filename_jinja_template:
            if hasattr(ti, 'task'):
                jinja_context = ti.get_template_context()
                jinja_context['try_number'] = try_number
            else:
                jinja_context = {
                    'ti': ti,
                    'ts': ti.execution_date.isoformat(),
                    'try_number': try_number,
                }
            return self.filename_jinja_template.render(**jinja_context)

        return self.filename_template.format(dag_id=ti.dag_id,
                                             task_id=ti.task_id,
                                             execution_date=ti.execution_date.isoformat(),
                                             try_number=try_number)

    def _read_grouped_logs(self):
        return False

    def _read(self, ti, try_number, metadata=None):  # pylint: disable=unused-argument
        """
        Template method that contains custom logic of reading
        logs given the try_number.
        :param ti: task instance record
        :param try_number: current try_number to read log from
        :param metadata: log metadata,
                         can be used for steaming log reading and auto-tailing.
        :return: log message as a string and metadata.
        """
        # Task instance here might be different from task instance when
        # initializing the handler. Thus explicitly getting log location
        # is needed to get correct log path.
        log_relative_path = self._render_filename(ti, try_number)
        location = os.path.join(self.local_base, log_relative_path)

        log = ""

        if os.path.exists(location):
            try:
                with open(location) as file:
                    log += "*** Reading local file: {}\n".format(location)
                    log += "".join(file.readlines())
            except Exception as e:  # pylint: disable=broad-except
                log = "*** Failed to load local log file: {}\n".format(location)
                log += "*** {}\n".format(str(e))
        elif conf.get('core', 'executor') == 'KubernetesExecutor':   # pylint: disable=too-many-nested-blocks
            try:
                from airflow.kubernetes.kube_client import get_kube_client

                kube_client = get_kube_client()

                pod_name = ti.hostname

                pod_list = kube_client.list_namespaced_pod(conf.get('kubernetes', 'namespace'))
                matches = [pod.metadata.name for pod in pod_list.items
                           # Second predicat not pod.metadata.labels.get('kubernetes_pod_operator') exclude pods that is deployed based on kubernetes_pod_operator
                           # because the logging is fullfilled based on parent pod.
                           if pod.metadata.labels.get('dag_id') and not pod.metadata.labels.get('kubernetes_pod_operator')
                           and ti.dag_id == pod.spec.containers[0].command[2]
                           and ti.task_id == pod.spec.containers[0].command[3]
                           # Kubenetes returns following execution_date format in string `2020-10-27T04_30_00_plus_00_00`
                           and ti.execution_date.strftime("%Y-%m-%dT%H_%M_%S_plus_00_00") == pod.metadata.labels['execution_date']
                           # Kubernetes returns try_number as a string
                           and str(ti.try_number) == pod.metadata.labels['try_number']]
                if len(matches) == 1:
                    pod_name = matches[0]

                log += '*** Trying to get logs (last 1000 lines) from worker pod {} ***\n\n'\
                    .format(pod_name)
                res = kube_client.read_namespaced_pod_log(
                    name=pod_name,
                    namespace=conf.get('kubernetes', 'namespace'),
                    container='base',
                    follow=False,
                    tail_lines=1000,
                    _preload_content=False
                )

                for line in res:
                    log += line.decode()

            except Exception as f:  # pylint: disable=broad-except
                log += '*** Unable to fetch logs from worker pod {} ***\n{}\n\n'.format(
                    ti.hostname, str(f)
                )
        else:
            url = os.path.join(
                "http://{ti.hostname}:{worker_log_server_port}/log", log_relative_path
            ).format(
                ti=ti,
                worker_log_server_port=conf.get('celery', 'WORKER_LOG_SERVER_PORT')
            )
            log += "*** Log file does not exist: {}\n".format(location)
            log += "*** Fetching from: {}\n".format(url)
            try:
                timeout = None  # No timeout
                try:
                    timeout = conf.getint('webserver', 'log_fetch_timeout_sec')
                except (AirflowConfigException, ValueError):
                    pass

                response = requests.get(url, timeout=timeout)
                response.encoding = "utf-8"

                # Check if the resource was properly fetched
                response.raise_for_status()

                log += '\n' + response.text
            except Exception as e:  # pylint: disable=broad-except
                log += "*** Failed to fetch log file from worker. {}\n".format(str(e))

        return log, {'end_of_log': True}

    def read(self, task_instance, try_number=None, metadata=None):
        """
        Read logs of given task instance from local machine.
        :param task_instance: task instance object
        :param try_number: task instance try_number to read logs from. If None
                           it returns all logs separated by try_number
        :param metadata: log metadata,
                         can be used for steaming log reading and auto-tailing.
        :return: a list of listed tuples which order log string by host
        """
        # Task instance increments its try number when it starts to run.
        # So the log for a particular task try will only show up when
        # try number gets incremented in DB, i.e logs produced the time
        # after cli run and before try_number + 1 in DB will not be displayed.

        if try_number is None:
            next_try = task_instance.next_try_number
            try_numbers = list(range(1, next_try))
        elif try_number < 1:
            logs = [
                [('default_host', 'Error fetching the logs. Try number {} is invalid.'.format(try_number))],
            ]
            return logs
        else:
            try_numbers = [try_number]

        logs = [''] * len(try_numbers)
        metadata_array = [{}] * len(try_numbers)
        for i, try_number_element in enumerate(try_numbers):
            log, metadata = self._read(task_instance, try_number_element, metadata)
            # es_task_handler return logs grouped by host. wrap other handler returning log string
            # with default/ empty host so that UI can render the response in the same way
            logs[i] = log if self._read_grouped_logs() else [(task_instance.hostname, log)]
            metadata_array[i] = metadata

        return logs, metadata_array

    def _init_file(self, ti):
        """
        Create log directory and give it correct permissions.
        :param ti: task instance object
        :return: relative log path of the given task instance
        """
        # To handle log writing when tasks are impersonated, the log files need to
        # be writable by the user that runs the Airflow command and the user
        # that is impersonated. This is mainly to handle corner cases with the
        # SubDagOperator. When the SubDagOperator is run, all of the operators
        # run under the impersonated user and create appropriate log files
        # as the impersonated user. However, if the user manually runs tasks
        # of the SubDagOperator through the UI, then the log files are created
        # by the user that runs the Airflow command. For example, the Airflow
        # run command may be run by the `airflow_sudoable` user, but the Airflow
        # tasks may be run by the `airflow` user. If the log files are not
        # writable by both users, then it's possible that re-running a task
        # via the UI (or vice versa) results in a permission error as the task
        # tries to write to a log file created by the other user.
        relative_path = self._render_filename(ti, ti.try_number)
        full_path = os.path.join(self.local_base, relative_path)
        directory = os.path.dirname(full_path)
        # Create the log file and give it group writable permissions
        # TODO(aoen): Make log dirs and logs globally readable for now since the SubDag
        # operator is not compatible with impersonation (e.g. if a Celery executor is used
        # for a SubDag operator and the SubDag operator has a different owner than the
        # parent DAG)
        Path(directory).mkdir(mode=0o777, parents=True, exist_ok=True)

        if not os.path.exists(full_path):
            open(full_path, "a").close()
            # TODO: Investigate using 444 instead of 666.
            os.chmod(full_path, 0o666)

        return full_path