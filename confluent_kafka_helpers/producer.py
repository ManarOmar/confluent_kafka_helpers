import atexit
from enum import Enum
import socket

import structlog
from confluent_kafka.avro import AvroProducer as ConfluentAvroProducer

from confluent_kafka_helpers.callbacks import (
    default_error_cb, default_on_delivery_cb, default_stats_cb, get_callback)
from confluent_kafka_helpers.schema_registry import (
    AvroSchemaRegistry, SchemaNotFound)

logger = structlog.get_logger(__name__)


class SubjectNameStrategy(Enum):
    TopicNameStrategy = 0
    RecordNameStrategy = 1
    TopicRecordNameStrategy = 2


class TopicNotRegistered(Exception):
    """
    Raised when someone tries to produce with a topic that
    hasn't been registered in the 'topics' configuration.
    """
    pass


class AvroProducer(ConfluentAvroProducer):

    DEFAULT_CONFIG = {
        'acks': 'all',
        'api.version.request': True,
        'client.id': socket.gethostname(),
        'log.connection.close': False,
        'max.in.flight': 1,
        'queue.buffering.max.ms': 100,
        'statistics.interval.ms': 15000,
        'auto.register.schemas': False,
        'key.subject.name.strategy': SubjectNameStrategy.TopicNameStrategy,
        'value.subject.name.strategy': SubjectNameStrategy.TopicNameStrategy
    }

    def __init__(self, config, value_serializer=None,
                 schema_registry=AvroSchemaRegistry,
                 get_callback=get_callback):  # yapf: disable
        config = {**self.DEFAULT_CONFIG, **config}
        config['on_delivery'] = get_callback(
            config.pop('on_delivery', None), default_on_delivery_cb
        )
        config['error_cb'] = get_callback(
            config.pop('error_cb', None), default_error_cb
        )
        config['stats_cb'] = get_callback(
            config.pop('stats_cb', None), default_stats_cb
        )

        schema_registry_url = config['schema.registry.url']
        self.schema_registry = schema_registry(schema_registry_url)
        self.value_serializer = config.pop('value_serializer', value_serializer)

        self.auto_register_schemas = config.pop('auto.register.schemas')
        self.key_subject_name_strategy = config.pop(
            'key.subject.name.strategy'
        )
        self.value_subject_name_strategy = config.pop(
            'value.subject.name.strategy'
        )

        topics = config.pop('topics')
        self.topic_schemas = self._get_topic_schemas(topics)

        # use the first topic as default
        default_topic_schema = next(iter(self.topic_schemas.values()))
        self.default_topic, *_ = default_topic_schema

        logger.info("Initializing producer", config=config)
        atexit.register(self._close)

        super().__init__(config)

    def _close(self):
        logger.info("Flushing producer")
        super().flush()

    def _get_subject_names(self, topic):
        """
        Get subject names for given topic.
        """
        key_subject_name = f'{topic}-key'
        value_subject_name = f'{topic}-value'
        return key_subject_name, value_subject_name

    def _get_topic_schemas(self, topics):
        """
        Get schemas for all topics.
        """
        topic_schemas = {}
        for topic in topics:
            key_name, value_name = self._get_subject_names(topic)
            try:
                key_schema = self.schema_registry.get_latest_schema(key_name)
            except SchemaNotFound:
                # topics that are used for only pub/sub will probably not
                # have a key set on the messages.
                #
                # on these topics we should not require a key schema.
                key_schema = None
            value_schema = self.schema_registry.get_latest_schema(value_name)
            topic_schemas[topic] = (topic, key_schema, value_schema)

        return topic_schemas

    def _get_subject(self, topic, schema, strategy):
        if strategy == SubjectNameStrategy.TopicNameStrategy:
            return topic
        elif strategy == SubjectNameStrategy.RecordNameStrategy:
            return schema.fullname
        elif strategy == SubjectNameStrategy.TopicRecordNameStrategy:
            return '%{}-%{}'.format(topic, schema.fullname)
        else:
            raise ValueError('Unknown SubjectNameStrategy')

    def _ensure_schemas(self, topic, key_schema, value_schema):
        key_subject = self._get_subject(
            topic, key_schema, self.key_subject_name_strategy) + '-key'
        value_subject = self._get_subject(
            topic, value_schema, self.value_subject_name_strategy) + '-value'

        if self.auto_register_schemas:
            key_schema = self.schema_registry.register_schema(
                key_subject, key_schema)
            value_schema = self.schema_registry.register_schema(
                value_subject, value_schema)
        else:
            key_schema = self.schema_registry.get_latest_schema(key_subject)
            value_schema = self.schema_registry.get_latest_schema(value_subject)

        return key_schema, value_schema

    def produce(self, value, key=None, topic=None,
                value_schema=None, key_schema=None, **kwargs):
        topic = topic or self.default_topic
        key_schema, value_schema = self._ensure_schemas(
                topic, key_schema, value_schema)

        if self.value_serializer:
            value = self.value_serializer(value)

        logger.info("Producing message", topic=topic, key=key, value=value)
        super().produce(
            topic=topic, key=key, value=value, key_schema=key_schema,
            value_schema=value_schema, **kwargs
        )
