from resp3 import Connection


redis = Connection()
result = redis.call("set", "foo", "bar")
print(result)
result = redis.call("get", "foo")
print(result)
