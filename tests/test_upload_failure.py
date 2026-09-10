import pytest
from test_pipeline import FakeMedia, FakeRemote

from photos_shrink.auth import BrowserAuthError, UploadNotStartedError
from photos_shrink.config import load_config
from photos_shrink.pipeline import Pipeline
from photos_shrink.state import StateStore


@pytest.mark.parametrize('error,stage', [
    (UploadNotStartedError('no upload control'), 'encoded'),
    (BrowserAuthError('submission outcome unknown'), 'upload_intent'),
])
def test_only_definite_pre_submission_failure_can_retry(tmp_path, error, stage):
    config = tmp_path / 'shrink.toml'
    config.write_text("[run]\nwork_dir='work'\npause_seconds=0\n", encoding='utf-8')
    settings = load_config(config)

    class Remote(FakeRemote):
        def upload(self, path):
            raise error

    remote = Remote(tmp_path)
    with StateStore(tmp_path / 'state.sqlite', 'account', settings.fingerprint) as state:
        with pytest.raises(type(error)):
            Pipeline(settings, remote, state, media=FakeMedia()).run(yes=True, keep_originals=True)
        assert state.get_item('orig')['stage'] == stage
    assert remote.trashed == 0
