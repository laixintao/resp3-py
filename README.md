# resp3-py

(WIP) A python implementation of RESP3.

### Install

```
pip install resp3
```

### Usage

```
from resp3 import Connection

redis = Connection()
result = redis.call("set", "foo", "bar")
print(result)
result = redis.call("get", "foo")
print(result)
```
