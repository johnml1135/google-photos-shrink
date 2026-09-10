from test_pipeline import FakeMedia, FakeRemote

from photos_shrink.config import load_config
from photos_shrink.pipeline import Pipeline
from photos_shrink.state import StateStore


def test_pipeline_passes_google_location_to_encoder(tmp_path):
    config = tmp_path / "shrink.toml"
    config.write_text("[run]\nwork_dir='work'\npause_seconds=0\n", encoding="utf-8")
    settings = load_config(config)
    remote = FakeRemote(tmp_path)
    remote.items[0]["metadata"].update(latitude=40.1, longitude=-73.1)

    class Media(FakeMedia):
        def encode(self, source, destination, settings):
            assert settings["source_metadata"]["latitude"] == 40.1
            assert settings["source_metadata"]["longitude"] == -73.1
            return super().encode(source, destination, settings)

    with StateStore(tmp_path / "state.sqlite", "account", settings.fingerprint) as state:
        Pipeline(settings, remote, state, media=Media()).run(yes=True, keep_originals=True)
    assert remote.uploads == 1
    assert remote.trashed == 0


def test_one_item_pilot_resumes_without_uploading_another_item(tmp_path):
    config = tmp_path / "shrink.toml"
    config.write_text("[run]\nwork_dir='work'\npause_seconds=0\nlimit=1\n", encoding="utf-8")
    settings = load_config(config)
    remote = FakeRemote(tmp_path)
    with StateStore(tmp_path / "state.sqlite", "account", settings.fingerprint) as state:
        pipeline = Pipeline(settings, remote, state, media=FakeMedia())
        pipeline.run(yes=True, keep_originals=True)
        remote.items.append({**remote.items[0], "id": "another", "dedup_key": "another-key"})
        pipeline.run(yes=True, keep_originals=True)
    assert remote.uploads == 1
    assert remote.trashed == 0
