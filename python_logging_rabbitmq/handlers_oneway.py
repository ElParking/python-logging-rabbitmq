# -*- coding: utf-8 -*-
import logging
import threading


import pika
from pika import credentials

from .filters import FieldFilter
from .formatters import JSONFormatter
from .compat import Queue


class RabbitMQHandlerOneWay(logging.Handler):
    """
    Python/Django logging handler to ship logs to RabbitMQ.
    Inspired by: https://github.com/ziXiong/MQHandler
    """

    def __init__(self, level=logging.NOTSET, formatter=JSONFormatter(),
                 host='localhost', port=5672, connection_params=None,
                 username=None, password=None,
                 exchange='log', declare_exchange=False,
                 close_after_emit=False,
                 fields=None, fields_under_root=True):
        """
        Initialize the handler.

        :param level:             Logs level.
        :param formatter:         Use custom formatter for the logs.
        :param host:              RabbitMQ host. Default localhost
        :param port:              RabbitMQ Port. Default 5672
        :param connection_params: Allow extra params to connect with RabbitMQ.
        :param username:          Username in case of authentication.
        :param password:          Password for the username.
        :param exchange:          Send logs using this exchange.
        :param declare_exchange:  Whether or not to declare the exchange.
        :param close_after_emit:  Close connection after emit the record?
        :param fields:            Send these fields as part of all logs.
        :param fields_under_root: Merge the fields in the root object.
        """

        super(RabbitMQHandlerOneWay, self).__init__(level=level)

        # Important instances/properties.
        self.exchange = exchange
        self.connection = None
        self.channel = None
        self.exchange_declared = not declare_exchange
        self.close_after_emit = close_after_emit

        # Connection parameters.
        # Allow extra params when connect to RabbitMQ.
        # @see: http://pika.readthedocs.io/en/0.10.0/modules/parameters.html#pika.connection.ConnectionParameters
        conn_params = connection_params if isinstance(connection_params, dict) else {}
        self.connection_params = conn_params.copy()
        self.connection_params.update(dict(host=host, port=port, heartbeat_interval=0))

        if username and password:
            self.connection_params['credentials'] = credentials.PlainCredentials(username, password)

        # Logging.
        self.formatter = formatter
        self.fields = fields if isinstance(fields, dict) else {}
        self.fields_under_root = fields_under_root

        if len(self.fields) > 0:
            self.addFilter(FieldFilter(self.fields, self.fields_under_root))

        # Connect.
        self.createLock()

        # message queue
        self.queue = Queue()
        self.start_message_worker()

    def open_connection(self):
        """
        Connect to RabbitMQ.
        """

        # Set logger for pika.
        # See if something went wrong connecting to RabbitMQ.
        if not self.connection or self.connection.is_closed or not self.channel or self.channel.is_closed:
            handler = logging.StreamHandler()
            handler.setFormatter(self.formatter)
            rabbitmq_logger = logging.getLogger('pika')
            rabbitmq_logger.addHandler(handler)
            rabbitmq_logger.propagate = False
            rabbitmq_logger.setLevel(logging.WARNING)

            # Connect.
            if not self.connection or self.connection.is_closed:
                self.connection = pika.BlockingConnection(pika.ConnectionParameters(**self.connection_params))

            if not self.channel or self.channel.is_closed:
                self.channel = self.connection.channel()

            if self.exchange_declared is False:
                self.channel.exchange_declare(exchange=self.exchange, type='topic', durable=True, auto_delete=False)
                self.exchange_declared = True

            # Manually remove logger to avoid shutdown message.
            rabbitmq_logger.removeHandler(handler)

    def close_connection(self):
        """
        Close active connection.
        """

        if self.channel:
            self.channel.close()

        if self.connection:
            self.connection.close()

        self.connection, self.channel = None, None

    def start_message_worker(self):
        worker = threading.Thread(target=self.message_worker)
        worker.setDaemon(True)
        worker.start()

    def message_worker(self):
        while 1:
            try:
                record, routing_key = self.queue.get()

                if not self.connection or self.connection.is_closed or not self.channel or self.channel.is_closed:
                    self.open_connection()

                self.channel.basic_publish(
                    exchange=self.exchange,
                    routing_key=routing_key,
                    body=self.format(record),
                    properties=pika.BasicProperties(
                        delivery_mode=2
                    )
                )
            except Exception:
                self.channel, self.connection = None, None
                self.handleError(record)
            finally:
                self.queue.task_done()
                if self.close_after_emit:
                    self.close_connection()

    def emit(self, record):
        try:
            routing_key = "{name}.{level}".format(name=record.name, level=record.levelname)
            self.queue.put((record, routing_key))
        except Exception:
            self.channel, self.connection = None, None
            self.handleError(record)

    def close(self):
        """
        Free resources.
        """

        self.acquire()

        try:
            self.close_connection()
        finally:
            self.release()
