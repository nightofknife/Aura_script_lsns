# packages/aura_core/utils/id_generator.py
import time
import threading


class SnowflakeGenerator:
    def __init__(self, instance=0, epoch=1609459200000):  # 2021-01-01
        self.instance = instance
        self.epoch = epoch
        self.sequence = 0
        self.last_timestamp = -1
        self.lock = threading.Lock()

        self.INSTANCE_BITS = 10
        self.SEQUENCE_BITS = 12
        self.TIMESTAMP_SHIFT = self.INSTANCE_BITS + self.SEQUENCE_BITS
        self.INSTANCE_SHIFT = self.SEQUENCE_BITS
        self.MAX_INSTANCE = -1 ^ (-1 << self.INSTANCE_BITS)
        self.MAX_SEQUENCE = -1 ^ (-1 << self.SEQUENCE_BITS)

        if self.instance > self.MAX_INSTANCE or self.instance < 0:
            raise ValueError(f"Instance ID can't be greater than {self.MAX_INSTANCE} or less than 0")

    def _current_millis(self):
        return int(time.time() * 1000)

    def _wait_for_next_millis(self, last_timestamp):
        timestamp = self._current_millis()
        while timestamp <= last_timestamp:
            timestamp = self._current_millis()
        return timestamp

    def __iter__(self):
        return self

    def __next__(self):
        with self.lock:
            timestamp = self._current_millis()
            if timestamp < self.last_timestamp:
                raise Exception("Clock moved backwards. Refusing to generate id for %d milliseconds" % (
                            self.last_timestamp - timestamp))

            if self.last_timestamp == timestamp:
                self.sequence = (self.sequence + 1) & self.MAX_SEQUENCE
                if self.sequence == 0:
                    timestamp = self._wait_for_next_millis(self.last_timestamp)
            else:
                self.sequence = 0

            self.last_timestamp = timestamp

            new_id = ((timestamp - self.epoch) << self.TIMESTAMP_SHIFT) | \
                     (self.instance << self.INSTANCE_SHIFT) | \
                     self.sequence
            return new_id

# 可以在 scheduler.py 中实例化
# id_generator = SnowflakeGenerator(instance=1)
