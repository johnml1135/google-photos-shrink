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
    GetTrashPage = _payload("GetTrashPage", "zy0IHe")
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

    def test_an_unanswered_item_is_asked_again_on_its_own(self):
        """Live: a batched request came back missing an item's extended info."""

        items = {key: info_and_ext(key) for key in ("a", "b")}
        dropped = []

        def send(payloads):
            calls = payloads if isinstance(payloads, list) else [payloads]
            responses = []
            for payload in calls:
                key = payload.args[0]
                if len(calls) > 2 and key == "b" and isinstance(payload, Payloads.GetItemInfoExt):
                    dropped.append(key)
                    continue
                data = items[key][0 if isinstance(payload, Payloads.GetItemInfo) else 1]
                responses.append(SimpleNamespace(response_id=payload.payload_id, success=True, data=data))
            return responses

        client = BatchClient()
        client.send_api_request = send
        result = remote_with(client).get_items(["a", "b"])
        assert dropped == ["b"]
        assert result["b"]["id"] == "b"

    def test_a_missing_offset_in_the_extended_info_falls_back_to_the_basic_info(self):
        """The live case: API uploads report no offset in the extended info."""

        info, ext = info_and_ext("a")
        info.timezone_offset, ext.timezone_offset = 0, None
        client = BatchClient(lambda p: info if isinstance(p, Payloads.GetItemInfo) else ext)
        assert remote_with(client).get_items(["a"])["a"]["timezone_offset"] == 0


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
    OTHER = {"id": "S", "dedup_key": "dS"}

    def test_each_kind_of_change_is_one_call_listing_every_item_in_its_own_request(self):
        """Live: eleven capture-time calls in one request got HTTP 400; one call listing items took."""

        client = BatchClient()
        trip = {"id": "trip", "title": "Trip", "shared": False}
        failures = remote_with(client).restore_many([
            {"replacement": self.REPLACEMENT, "timestamp": (1_700_000_000_521, -14_400_000),
             "albums": [trip], "favorite": True, "archived": False, "description": "beach"},
            {"replacement": self.OTHER, "timestamp": (1_600_000_000_000, 0), "albums": [trip], "favorite": True},
        ])
        assert failures == {}
        assert all(len(request) == 1 for request in client.requests)
        calls = {type(r[0]).__name__: r[0] for r in client.requests}
        assert len(client.requests) == len(calls)
        assert calls["SetItemTimestamp"].data == [[["dR", 1_700_000_000, -14_400], ["dS", 1_600_000_000, 0]]]
        assert calls["AddItemsToExistingAlbum"].args == (["R", "S"], "trip")
        assert calls["SetFavorite"].args == (["dR", "dS"],)
        assert calls["UnArchive"].args == (["dR"],)
        assert calls["SetItemDescription"].args == ("dR", "beach")

    def test_a_failed_call_is_reported_against_every_item_it_carried(self):
        client = BatchClient(lambda p: None if isinstance(p, Payloads.SetItemTimestamp) else True)
        failures = remote_with(client).restore_many([
            {"replacement": self.REPLACEMENT, "timestamp": (1000, 0)},
            {"replacement": self.OTHER, "timestamp": (2000, 0), "favorite": True},
        ])
        assert set(failures) == {"R", "S"}
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


class TestInBin:
    def _pages(self, *pages):
        def answer(payload):
            index = int(payload.args[0] or 0)
            items, more = pages[index]
            return SimpleNamespace(
                items=[SimpleNamespace(dedup_key=key) for key in items],
                next_page_id=str(index + 1) if more else None,
            )
        return BatchClient(answer)

    def test_pages_until_every_key_is_found(self):
        client = self._pages((["x", "a"], True), (["b"], True), (["never read"], False))
        assert remote_with(client).in_bin(["a", "b"]) == {"a", "b"}
        assert len(client.requests) == 2

    def test_reports_only_what_the_bin_holds(self):
        client = self._pages((["a"], False))
        assert remote_with(client).in_bin(["a", "b"]) == {"a"}
