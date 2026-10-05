"""Stand-ins for the AWS services proteinCAD uses.

    from fake_aws import FakeDynamo, FakeS3, FakeSqs, FakeEc2

Each one answers the handful of calls the code actually makes, in the shapes
boto3 uses, and records what it was asked for. Anything unimplemented raises
rather than returning a plausible empty answer -- a silent None here would show
up as a mysterious failure three assertions later.

They are in their own file because two things need them: tools/check_cloud.py,
which asserts against them, and the browser stub that puts the real Lambda code
behind an HTTP port so the hosted path can be driven on a laptop.

Deliberately not general. A parser that accepted more than the code writes
would stop being a check on what the code writes.
"""

from __future__ import annotations

import re

from proteincad import cloud_api


class Refused(Exception):
    """What DynamoDB raises when a ConditionExpression does not hold."""

    def __init__(self):
        super().__init__("The conditional request failed")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class Boom(Exception):
    """An AWS call that fails for some reason of its own."""


def _plain(attribute):
    return cloud_api._plain(attribute)


def _value(item, name):
    return _plain(item[name]) if name in item else None


class FakeDynamo:
    """A table with a primary key of (pk, sk) and one index, by_user.

    Understands the two update forms this codebase uses -- `SET a = :v` with
    escaped names, and `ADD counter :n` -- and the conditions written against
    them. Deliberately not a general implementation: a parser that accepted
    more than the code writes would stop being a check on what the code writes.
    """

    def __init__(self):
        self.items: dict = {}
        self.calls: list = []

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _key(key):
        return (_plain(key["pk"]), _plain(key["sk"]))

    def _resolve(self, name, names):
        return names.get(name, name) if names else name

    def _condition(self, text, item, values, names):
        """Evaluate the small grammar the code uses: comparisons and existence
        tests joined by AND / OR, left to right, no parentheses."""
        tokens = re.split(r"\s+(AND|OR)\s+", text.strip())
        result = self._term(tokens[0], item, values, names)
        index = 1
        while index < len(tokens) - 1:
            joiner, term = tokens[index], tokens[index + 1]
            value = self._term(term, item, values, names)
            result = (result and value) if joiner == "AND" else (result or value)
            index += 2
        return result

    def _term(self, text, item, values, names):
        text = text.strip()
        exists = re.match(r"attribute_(not_)?exists\(([^)]+)\)", text)
        if exists:
            name = self._resolve(exists.group(2).strip(), names)
            present = name in item
            return (not present) if exists.group(1) else present
        compare = re.match(r"(\S+)\s*(<=|>=|<>|=|<|>)\s*(:\w+)", text)
        if not compare:
            raise AssertionError(f"the stand-in cannot read the condition {text!r}")
        name = self._resolve(compare.group(1), names)
        if name not in item:
            return False
        left = _plain(item[name])
        right = _plain(values[compare.group(3)])
        op = compare.group(2)
        if op == "=":
            return left == right
        if op == "<>":
            return left != right
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op == ">":
            return left > right
        return left >= right

    # -- the api ----------------------------------------------------------

    def put_item(self, TableName, Item, **_):
        self.calls.append(("put_item", TableName))
        self.items[self._key(Item)] = dict(Item)

    def get_item(self, TableName, Key, **_):
        self.calls.append(("get_item", TableName))
        item = self.items.get(self._key(Key))
        return {"Item": dict(item)} if item else {}

    def update_item(self, TableName, Key, UpdateExpression,
                    ExpressionAttributeValues=None, ExpressionAttributeNames=None,
                    ConditionExpression=None, **_):
        self.calls.append(("update_item", TableName))
        where = self._key(Key)
        item = dict(self.items.get(where) or {})
        if not item:
            item = dict(Key)

        values = ExpressionAttributeValues or {}
        names = ExpressionAttributeNames or {}

        if ConditionExpression and not self._condition(ConditionExpression, item, values, names):
            raise Refused()

        for clause, body in self._clauses(UpdateExpression):
            if clause == "SET":
                for assignment in self._split(body):
                    left, right = assignment.split("=", 1)
                    name = self._resolve(left.strip(), names)
                    right = right.strip()
                    keep = re.match(r"if_not_exists\(([^,]+),\s*(:\w+)\)", right)
                    if keep:
                        if name in item:
                            continue
                        item[name] = values[keep.group(2)]
                    else:
                        item[name] = values[right]
            elif clause == "ADD":
                for addition in self._split(body):
                    left, right = addition.split()
                    name = self._resolve(left.strip(), names)
                    current = _plain(item[name]) if name in item else 0
                    item[name] = {"N": str(current + _plain(values[right.strip()]))}
            else:
                raise AssertionError(f"the stand-in cannot read {clause}")

        self.items[where] = item
        if _.get("ReturnValues") == "ALL_NEW":
            return {"Attributes": dict(item)}
        return {}

    @staticmethod
    def _clauses(expression):
        parts = re.split(r"\b(SET|ADD|REMOVE|DELETE)\b", expression)
        out = []
        for index in range(1, len(parts) - 1, 2):
            out.append((parts[index], parts[index + 1]))
        return out

    @staticmethod
    def _split(body):
        """Top-level commas only -- if_not_exists(expires, :ttl) is one piece."""
        pieces, depth, current = [], 0, ""
        for character in body:
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
            if character == "," and depth == 0:
                pieces.append(current)
                current = ""
            else:
                current += character
        pieces.append(current)
        return [piece.strip() for piece in pieces if piece.strip()]

    def query(self, TableName, KeyConditionExpression, ExpressionAttributeValues,
              IndexName=None, ScanIndexForward=True, Limit=40, **_):
        """Two shapes, because the code makes two kinds of query.

        `by_user` for somebody's job list, sorted by when it was made, and the
        base table by partition key for everything under pk=MACHINE. Anything
        else raises rather than guessing.
        """
        self.calls.append(("query", IndexName or "base"))
        wanted = _plain(list(ExpressionAttributeValues.values())[0])

        if IndexName == "by_user":
            found = [item for item in self.items.values()
                     if _value(item, "user_id") == wanted]
            found.sort(key=lambda item: _value(item, "created") or 0,
                       reverse=not ScanIndexForward)
        elif IndexName is None and KeyConditionExpression.strip().startswith("pk ="):
            found = [item for key, item in self.items.items() if key[0] == wanted]
            found.sort(key=lambda item: _value(item, "sk") or "")
        else:
            raise AssertionError(
                f"the stand-in cannot read the query {KeyConditionExpression!r} "
                f"on {IndexName!r}")
        return {"Items": [dict(item) for item in found[:Limit]]}


class FakeS3:
    def __init__(self):
        self.objects: dict = {}
        self.signed: list = []

    def put_object(self, Bucket, Key, Body, **_):
        self.objects[(Bucket, Key)] = Body if isinstance(Body, bytes) else str(Body).encode()

    def get_object(self, Bucket, Key, **_):
        if (Bucket, Key) not in self.objects:
            raise Boom(f"NoSuchKey: {Key}")
        return {"Body": self.objects[(Bucket, Key)]}

    def generate_presigned_url(self, operation, Params, ExpiresIn, **_):
        self.signed.append((operation, Params["Key"], ExpiresIn))
        return f"https://bucket.example/{Params['Key']}?X-Amz-Expires={ExpiresIn}"


class FakeSqs:
    def __init__(self):
        self.messages: list = []
        self.deleted: list = []
        self.extended: list = []
        self.visible = 0
        self.fail = False

    def send_message(self, QueueUrl, MessageBody, **_):
        if self.fail:
            raise Boom("queue unavailable")
        self.messages.append({"Body": MessageBody, "ReceiptHandle": "r%d" % len(self.messages)})
        return {"MessageId": "m%d" % len(self.messages)}

    def receive_message(self, QueueUrl, **_):
        return {"Messages": [self.messages.pop(0)]} if self.messages else {}

    def delete_message(self, QueueUrl, ReceiptHandle, **_):
        self.deleted.append(ReceiptHandle)

    def change_message_visibility(self, QueueUrl, ReceiptHandle, VisibilityTimeout, **_):
        self.extended.append((ReceiptHandle, VisibilityTimeout))

    def get_queue_attributes(self, QueueUrl, AttributeNames, **_):
        waiting = self.visible if self.visible else len(self.messages)
        return {"Attributes": {"ApproximateNumberOfMessagesVisible": str(waiting),
                               "ApproximateNumberOfMessagesNotVisible": "0"}}


class NoCapacity(Exception):
    """What EC2 says when a zone has no room for the instance type you asked
    for. Not an error in the request; an error in the world."""

    def __init__(self, code="InsufficientInstanceCapacity"):
        super().__init__(f"{code}: We currently do not have sufficient capacity")
        self.response = {"Error": {"Code": code}}


class FakeEc2:
    """Enough EC2 to launch an instance that does not exist.

    `full` names the subnets that have no capacity, which is how the zone
    fallback is exercised -- and how "all three are full" is exercised, which
    is the case that has to fail cleanly rather than quietly launch something
    bigger.

    `over_quota` is the other refusal, and a different thing entirely: an
    account limit, the same in every zone, which no amount of retrying fixes.
    """

    def __init__(self, state="stopped", full=(), over_quota=False):
        self.state = state
        self.calls: list = []
        self.full = set(full)
        self.over_quota = over_quota
        self.launched: list = []
        self.next_id = 1

    def run_instances(self, LaunchTemplate=None, SubnetId=None, MinCount=1,
                      MaxCount=1, TagSpecifications=None, **kwargs):
        self.calls.append(("run", SubnetId))
        if self.over_quota:
            raise NoCapacity("VcpuLimitExceeded")
        if SubnetId in self.full:
            raise NoCapacity()
        instance_id = "i-%012x" % self.next_id
        self.next_id += 1
        record = {
            "InstanceId": instance_id,
            "SubnetId": SubnetId,
            "LaunchTemplate": LaunchTemplate,
            "Placement": {"AvailabilityZone": (SubnetId or "subnet-x") + "z"},
            "Tags": [tag for spec in (TagSpecifications or []) for tag in spec["Tags"]],
            # Recorded so a test can prove nothing ever asks for a different
            # instance type when a zone is full.
            "Overrides": {k: v for k, v in kwargs.items()},
        }
        self.launched.append(record)
        self.state = "pending"
        return {"Instances": [record]}

    def start_instances(self, InstanceIds, **_):
        self.calls.append(("start", tuple(InstanceIds)))
        self.state = "pending"
        return {}

    def stop_instances(self, InstanceIds, **_):
        self.calls.append(("stop", tuple(InstanceIds)))
        self.state = "stopping"
        return {}

    def terminate_instances(self, InstanceIds, **_):
        self.calls.append(("terminate", tuple(InstanceIds)))
        self.state = "shutting-down"
        return {}

    def describe_instances(self, InstanceIds=None, **_):
        self.calls.append(("describe", tuple(InstanceIds or ())))
        return {"Reservations": [{"Instances": [{
            "InstanceId": (InstanceIds or ["i-test"])[0],
            "State": {"Name": self.state},
            "InstanceType": "g4dn.xlarge",
            "PublicIpAddress": "203.0.113.7",
            "PrivateIpAddress": "10.0.0.7",
        }]}]}
