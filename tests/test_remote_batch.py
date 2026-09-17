"""The batched calls the replace step makes: many calls per request, failures kept per call."""

from __future__ import annotations

import base64
import hashlib
import itertools
from types import SimpleNamespace

import pytest

from photos_shrink.remote import GooglePhotosRemote, RemoteProtocolError

_ids = itertools.count()


class _Payload:
    rpcid = "test"

    def __init__(self, *args):
        self.args = args
        self.payload_id = f"p{next(_ids)}"


def _payload(name: str, rpcid: str) -> type:
    return type(name, (_Payload,), {"rpcid": rpcid})


class Payloads:
    GetItemInfo = _payload("GetItemInfo", "VrseUb")
    GetItemInfoExt = _payload("GetItemInfoExt", "fDcn4b")
    GetRemoteMatchesByHash = _payload("GetRemoteMatchesByHash", "swbisb")
    MoveToTrash = _payload("MoveToTrash", "XwAOJf")
    SetItemTimestamp = _payload("SetItemTimestamp", "DaSgWe")
    SetItemDescription = _payload("SetItemDescription", "AQNOFd")
    SetFavorite = _payload("SetFavorite", "fav")
    UnFavorite = _payload("UnFavorite", "unfav")
    SetArchive = _payload("SetArchive", "arch")
    UnArchive = _payload("UnArchive", "unarch")
    AddItemsToExistingAlbum = _payload("AddItemsToExistingAlbum", "E1Cajb")
    AddItemsToExistingSharedAlbum = _payload("AddItemsToExistingSharedAlbum", "laUYf")


class BatchClient:
    """Answers every call in a request, in reverse order, as Google may.

    `answer(payload)` returns the data for a call, or None to make it fail.
    """

    def __init__(self, answer=lambda payload: True):
        self.answer = answer
        self.requests: list[list] = []
        self.session = SimpleNamespace()

    def send_api_request(self, payloads):
        # Like gpwc: a list gets a list back, a single call its one response.
        single = not isinstance(payloads, list)
        calls = [payloads] if single else payloads
        self.requests.append(calls)
        responses = []
        for payload in reversed(calls):
            data = self.answer(payload)
            responses.append(SimpleNamespace(response_id=payload.payload_id, success=data is not None, data=data))
        return responses[0] if single else responses


def remote_with(client, **run) -> GooglePhotosRemote:
    return GooglePhotosRemote(
        {"run": {"skip_shared": True, **run}, "google": {"session_refresh_seconds": 0}},
        client=client, payloads=Payloads,
    )


def info_and_ext(key: str, *, timestamp=1_700_000_000_000, albums=()):
    info = SimpleNamespace(
        media_key=key, dedup_key=f"dedup-{key}", video_duration=None, is_favorite=False,
        is_archived=False, is_partial_upload=False, live_photo_duration=None, space_taken=10,
        download_original_url="https://example.test/o", trash_timestamp=None,
    )
    ext = SimpleNamespace(
        media_key=key, dedup_key=f"dedup-{key}", file_name=f"{key}.jpg", size=10, res_width=4,
        res_height=3, timestamp=timestamp, timezone_offset=0, description_full=None, geo_location=None,
        albums=[SimpleNamespace(media_key=a, title=a, is_shared=False) for a in albums],
        source=["upload"], space_taken=10,
    )
    return info, ext


class TestExecuteMany:
    def test_responses_are_matched_to_calls_by_id_not_order(self):
        client = BatchClient(answer=lambda p: p.args[0])
        remote = remote_with(client)
        payloads = [Payloads.GetItemInfo("a"), Payloads.GetItemInfo("b"), Payloads.GetItemInfo("c")]
        assert remote._execute_many(payloads) == ["a", "b", "c"]
        assert len(client.requests) == 1

    def test_a_failed_call_fails_alone(self):
        client = BatchClient(answer=lambda p: None if p.args[0] == "b" else p.args[0])
        results = remote_with(client)._execute_many([Payloads.GetItemInfo("a"), Payloads.GetItemInfo("b")])
        assert results[0] == "a"
        assert isinstance(results[1], RemoteProtocolError)
        assert "rpc=VrseUb" in str(results[1])

    def test_a_missing_response_is_a_failure(self):
        client = BatchClient()
        client.send_api_request = lambda payloads: []
        (result,) = remote_with(client)._execute_many([Payloads.GetItemInfo("a")])
        assert isinstance(result, RemoteProtocolError)

    def test_a_failed_request_raises(self):
        client = BatchClient()

        def broken(payloads):
            raise RuntimeError("connection reset")

        client.send_api_request = broken
        with pytest.raises(RemoteProtocolError, match="request failed"):
            remote_with(client)._execute_many([Payloads.GetItemInfo("a")])


class TestGetItems:
    def test_reads_info_and_ext_for_many_keys_in_one_request(self):
        items = {key: info_and_ext(key, albums=["trip"]) for key in ("a", "b")}

        def answer(payload):
            info, ext = items[payload.args[0]]
            return info if isinstance(payload, Payloads.GetItemInfo) else ext

        client = BatchClient(answer)
        result = remote_with(client).get_items(["a", "b"])
        assert result["a"]["id"] == "a"
        assert result["b"]["dedup_key"] == "dedup-b"
        assert result["a"]["metadata"]["albums"] == [{"id": "trip", "title": "trip", "shared": False}]
        assert len(client.requests) == 1
        assert len(client.requests[0]) == 4

    def test_splits_into_requests_of_the_given_size(self):
        client = BatchClient(lambda p: info_and_ext(p.args[0])[0 if isinstance(p, Payloads.GetItemInfo) else 1])
        remote_with(client).get_items(["a", "b", "c"], per_request=2)
        assert [len(r) for r in client.requests] == [4, 2]

    def test_one_unreadable_item_does_not_spoil_the_others(self):
        def answer(payload):
            if payload.args[0] == "bad":
                return None
            return info_and_ext("good")[0 if isinstance(payload, Payloads.GetItemInfo) else 1]

        result = remote_with(BatchClient(answer)).get_items(["good", "bad"])
        assert result["good"]["id"] == "good"
        assert isinstance(result["bad"], RemoteProtocolError)

    def test_a_trashed_item_says_so(self):
        info, ext = info_and_ext("a")
        info.trash_timestamp = 123
        client = BatchClient(lambda p: info if isinstance(p, Payloads.GetItemInfo) else ext)
        assert remote_with(client).get_items(["a"])["a"]["trashed"] is True


def _hash(data: bytes) -> str:
    return base64.b64encode(hashlib.sha1(data).digest()).decode()


class TestFindUploadedMany:
    def test_resolves_found_absent_and_ambiguous_files_in_one_request(self, tmp_path):
        found, absent, twice = (tmp_path / n for n in ("found", "absent", "twice"))
        for path in (found, absent, twice):
            path.write_bytes(path.name.encode())

        def answer(payload):
            return [
                SimpleNamespace(hash=_hash(b"found"), media_key="F", dedup_key="dF"),
                SimpleNamespace(hash=_hash(b"twice"), media_key="T1", dedup_key="d1"),
                SimpleNamespace(hash=_hash(b"twice"), media_key="T2", dedup_key="d2"),
            ]

        client = BatchClient(answer)
        result = remote_with(client).find_uploaded_many([found, absent, twice])
        assert result[found] == {"id": "F", "dedup_key": "dF"}
        assert result[absent] is None
        assert isinstance(result[twice], RemoteProtocolError)
        assert len(client.requests) == 1
        assert sorted(client.requests[0][0].args[0]) == sorted([_hash(b"found"), _hash(b"absent"), _hash(b"twice")])

    def test_a_failed_lookup_fails_its_whole_chunk(self, tmp_path):
        path = tmp_path / "x"
        path.write_bytes(b"x")
        result = remote_with(BatchClient(lambda p: None)).find_uploaded_many([path])
        assert isinstance(result[path], RemoteProtocolError)


class TestRestoreMany:
    REPLACEMENT = {"id": "R", "dedup_key": "dR"}

    def test_builds_one_call_per_change_in_one_request(self):
        client = BatchClient()
        failures = remote_with(client).restore_many([{
            "replacement": self.REPLACEMENT,
            "timestamp": (1_700_000_000_000, -14_400_000),
            "albums": [{"id": "trip", "title": "Trip", "shared": False}],
            "favorite": True,
            "archived": False,
            "description": "beach",
        }])
        assert failures == {}
        (request,) = client.requests
        calls = {type(p).__name__: p.args for p in request}
        assert calls["SetItemTimestamp"] == ("dR", 1_700_000_000_000, -14_400)
        assert calls["AddItemsToExistingAlbum"] == (["R"], "trip")
        assert calls["SetFavorite"] == (["dR"],)
        assert calls["UnArchive"] == (["dR"],)
        assert calls["SetItemDescription"] == ("dR", "beach")

    def test_a_failed_call_is_reported_against_its_replacement(self):
        client = BatchClient(lambda p: None if isinstance(p, Payloads.SetItemTimestamp) else True)
        failures = remote_with(client).restore_many([{"replacement": self.REPLACEMENT, "timestamp": (1, 0)}])
        assert "rpc=DaSgWe" in str(failures["R"])

    def test_a_shared_album_is_refused_when_shared_albums_are_skipped(self):
        client = BatchClient()
        failures = remote_with(client).restore_many([
            {"replacement": self.REPLACEMENT, "albums": [{"id": "s", "title": "Family", "shared": True}]}
        ])
        assert "shared album" in str(failures["R"])
        assert client.requests == []

    def test_a_fractional_second_offset_is_refused(self):
        failures = remote_with(BatchClient()).restore_many([{"replacement": self.REPLACEMENT, "timestamp": (1, 1500)}])
        assert "whole seconds" in str(failures["R"])


class TestTrashMany:
    def test_trashes_up_to_the_request_size_per_call(self):
        client = BatchClient()
        remote_with(client).trash_many([f"d{i}" for i in range(250)])
        assert [len(r[0].args[0]) for r in client.requests] == [100, 100, 50]

    def test_an_empty_key_refuses_the_whole_call(self):
        client = BatchClient()
        with pytest.raises(RemoteProtocolError, match="incomplete identity"):
            remote_with(client).trash_many(["d1", ""])
        assert client.requests == []
