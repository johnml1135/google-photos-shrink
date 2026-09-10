from __future__ import annotations

import pytest

from photos_shrink.remote import GooglePhotosRemote, RemoteProtocolError


class Payloads:
    class SetItemTimestamp:
        def __init__(self, *args):
            self.args = args

    class SetFavorite:
        def __init__(self, *args):
            self.args = args

    class UnFavorite(SetFavorite):
        pass

    class SetArchive(SetFavorite):
        pass

    class UnArchive(SetFavorite):
        pass

    class SetItemDescription(SetFavorite):
        pass

    class AddItemsToExistingAlbum(SetFavorite):
        pass

    class DeleteItemGeoData:
        def __init__(self, keys):
            self.keys = keys


class Client:
    def __init__(self):
        self.calls = []

    def send_api_request(self, payload):
        self.calls.append(payload)
        return type("Response", (), {"success": True, "data": []})()


def metadata(latitude=40.0, longitude=-73.0):
    return {
        "latitude": latitude,
        "longitude": longitude,
    }


def test_restore_location_preserves_coordinates_without_fabricating_wire_fields():
    client = Client()
    remote = GooglePhotosRemote({}, client=client, payloads=Payloads)
    original = {"timestamp_ms": 1, "timezone_offset": 0, "metadata": {
        **metadata(), "description": None, "favorite": False, "archived": False, "albums": []
    }}

    remote.restore_metadata(original, {"id": "new", "dedup_key": "dedup"})

    assert not any(type(call).__name__ == "SetItemGeoData" for call in client.calls)


@pytest.mark.parametrize("field, value", [("latitude", 91), ("longitude", 181), ("latitude", float("nan"))])
def test_restore_location_rejects_invalid_coordinates(field, value):
    md = metadata()
    md[field] = value
    remote = GooglePhotosRemote({}, payloads=Payloads, client=Client())
    original = {"timestamp_ms": 1, "timezone_offset": 0, "metadata": {
        **md, "description": None, "favorite": False, "archived": False, "albums": []
    }}

    with pytest.raises(RemoteProtocolError, match="location"):
        remote.restore_metadata(original, {"id": "new", "dedup_key": "dedup"})


def test_restore_location_preserves_coordinates_when_wire_metadata_is_unavailable():
    md = metadata()
    remote = GooglePhotosRemote({}, payloads=Payloads, client=Client())
    original = {"timestamp_ms": 1, "timezone_offset": 0, "metadata": {
        **md, "description": None, "favorite": False, "archived": False, "albums": []
    }}

    remote.restore_metadata(original, {"id": "new", "dedup_key": "dedup"})
    assert not any(type(call).__name__ == "SetItemGeoData" for call in remote._client.calls)


def test_restore_location_deletes_unexpected_replacement_location():
    md = {**metadata(None, None), "description": None, "favorite": False, "archived": False, "albums": []}
    replacement_md = {**metadata(40.0, -73.0)}
    remote = GooglePhotosRemote({}, payloads=Payloads, client=Client())
    remote.restore_metadata(
        {"timestamp_ms": 1, "timezone_offset": 0, "metadata": md},
        {"id": "new", "dedup_key": "dedup", "metadata": replacement_md},
    )
    delete = [call for call in remote._client.calls if isinstance(call, Payloads.DeleteItemGeoData)]
    assert len(delete) == 1 and delete[0].keys == ["dedup"]


def test_restore_metadata_is_idempotent_when_replacement_already_matches():
    md = {**metadata(None, None), "description": "caption", "favorite": True, "archived": False, "albums": []}
    original = {"timestamp_ms": 1700000000000, "timezone_offset": -14400000, "metadata": md}
    replacement = {"id": "new", "dedup_key": "dedup", "timestamp_ms": original["timestamp_ms"],
                   "timezone_offset": original["timezone_offset"], "metadata": md}
    client = Client()
    GooglePhotosRemote({}, payloads=Payloads, client=client).restore_metadata(original, replacement)
    assert client.calls == []


def test_restore_timestamp_converts_millisecond_timezone_to_seconds():
    md = {**metadata(None, None), "description": None, "favorite": False, "archived": False, "albums": []}
    client = Client()
    GooglePhotosRemote({}, payloads=Payloads, client=client).restore_metadata(
        {"timestamp_ms": 1700000000000, "timezone_offset": -14400000, "metadata": md},
        {"id": "new", "dedup_key": "dedup"},
    )
    timestamp = next(call for call in client.calls if isinstance(call, Payloads.SetItemTimestamp))
    assert timestamp.args == ("dedup", 1700000000000, -14400)


def test_verify_replacement_checks_location_presence_and_value(tmp_path):
    md = {**metadata(), "description": None, "favorite": False, "archived": False, "albums": []}
    original = {"id": "old", "dedup_key": "old-key", "timestamp_ms": 1, "timezone_offset": 0, "metadata": md}
    replacement = {"id": "new", "dedup_key": "new-key", "size_bytes": 1, "width": 1, "height": 1, "kind": "photo"}

    output = tmp_path / "encoded.jpg"
    output.write_bytes(b"x")
    digest = GooglePhotosRemote._sha256(output)

    class Remote(GooglePhotosRemote):
        def get_item(self, item_id):
            return {**replacement, "timestamp_ms": 1, "timezone_offset": 0, "metadata": {
                **md, "longitude": -72.0
            }, "skip_reason": None}

        def _verify_remote_bytes(self, item, expected_sha256):
            return None

    remote = Remote({})
    with pytest.raises(RemoteProtocolError, match="location"):
        remote.verify_replacement(original, {**replacement, "sha256": digest}, {
            "size_bytes": 1, "width": 1, "height": 1, "kind": "photo", "sha256": digest, "path": output
        })


def test_location_roundtrip_tolerates_exif_float_rounding(tmp_path):
    output = tmp_path / "encoded.jpg"
    output.write_bytes(b"x")
    digest = GooglePhotosRemote._sha256(output)
    md = {**metadata(), "description": None, "favorite": False, "archived": False, "albums": []}
    original = {"id": "old", "dedup_key": "old-key", "timestamp_ms": 1, "timezone_offset": 0, "metadata": md}
    replacement = {"id": "new", "dedup_key": "new-key", "size_bytes": 1, "width": 1, "height": 1, "kind": "photo", "sha256": digest}

    class Remote(GooglePhotosRemote):
        def get_item(self, item_id):
            return {**replacement, "timestamp_ms": 1, "timezone_offset": 0, "metadata": {
                **md, "latitude": 40.00000005, "longitude": -73.00000005
            }, "skip_reason": None}

        def _verify_remote_bytes(self, item, expected_sha256):
            return None

    Remote({}).verify_replacement(original, replacement, {
        "size_bytes": 1, "width": 1, "height": 1, "kind": "photo", "sha256": digest, "path": output
    })
