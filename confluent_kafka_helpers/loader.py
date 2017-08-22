import zlib

from confluent_kafka import KafkaError, KafkaException, TopicPartition
from confluent_kafka.avro import AvroConsumer
from confluent_kafka.avro.cached_schema_registry_client import (
    CachedSchemaRegistryClient)
from confluent_kafka.avro.serializer.message_serializer import MessageSerializer

from confluent_kafka_helpers import logger


def default_partitioner(key, num_partitions):
    """
    Algorithm used in Kafkas default 'consistent' partitioner
    """
    return zlib.crc32(key) % num_partitions


class AvroMessageLoader:

    def __init__(self, consumer_config, schema_registry_url, topic,
                 default_key_schema, num_partitions):
        # TODO: make sure we only create the consumer once
        self.consumer = AvroConsumer(consumer_config)

        self.default_key_schema = default_key_schema
        self.num_partitions = num_partitions
        self.schema_registry_url = schema_registry_url
        self.topic = topic

    def _serialize_avro_key(self, key):
        schema_registry = CachedSchemaRegistryClient(
            url=self.schema_registry_url
        )
        serializer = MessageSerializer(schema_registry)
        key = serializer.encode_record_with_schema(
            self.topic, self.default_key_schema, key, is_key=True
        )
        return key

    def load(self, key, key_filter=lambda *args, **kwargs: True,
             partitioner=default_partitioner):
        """
        Load all historical messages for a key.

        Args:
            key: Key used when the event was stored, probably the
                ID of the message.
            key_filter: Callable used to filter the key. Usually we
                are only interested in messages with the same keys.

        Raises:
            KafkaException

        Returns:
            list: A list with all historical messages.
        """
        # since all messages with the same key are guaranteed to be stored
        #    in the same topic partition (using default partitioner) we can
        #    optimize the loading by only reading from that partition.
        #
        # if we know the key and total number of partitions we can
        #     deterministically calculate the partition number that was used.
        serialized_key = self._serialize_avro_key(key)
        partition_num = self._get_partition(serialized_key)
        partition = TopicPartition(self.topic, partition_num, 0)

        self.consumer.assign([partition])
        _, max_offset = self.consumer.get_watermark_offsets(
            partition, timeout=0.5, cached=False
        )
        logger.info("Loading aggregate from repository",
                    partition=partition, max_offset=max_offset)

        messages = []
        running = True
        try:
            # make sure we don't poll an empty partition since we will
            #     get stuck in an infinite loop
            while running and max_offset != 0:
                message = self.consumer.poll(timeout=0.1)
                if not message:
                    continue

                if message.error():
                    # TODO: investigate why this is not working
                    if message.error().code() == KafkaError._PARTITION_EOF:
                        running = False
                    else:
                        raise KafkaException(message.error())

                _, max_offset = self.consumer.get_watermark_offsets(
                    partition, timeout=0.5, cached=False
                )
                message_key = message.key()
                message_value = message.value()
                message_offset = message.offset()

                if key_filter(key, message_key):
                    logger.info("Loaded message", key=message_key,
                                message=message_value, offset=message_offset)
                    messages.append(message_value)

                # use this "hack" until KafkaError._PARTITION_EOF works
                if message_offset + 1 == max_offset:
                    logger.info("Reached partition EOF")
                    running = False

        except KeyboardInterrupt:
            print("Aborted")

        # We are not able to reuse the Consumer if we close the connection
        # https://github.com/confluentinc/confluent-kafka-python/issues/204
        # consumer.close()

        return messages