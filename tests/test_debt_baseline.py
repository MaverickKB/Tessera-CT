"""Platform-tagged debt stays in the baseline only on the platform that has it."""

import pytest

from debt_baseline import assert_matches_baseline


def baseline(tmp_path):
    path = tmp_path / 'baseline.txt'
    path.write_text(
        'always # shared debt\n'
        'mac-only # platform=darwin Default and Native are the same backend on macOS\n',
        encoding='utf-8')
    return path


def test_a_platform_tag_is_debt_only_on_that_platform(tmp_path):
    path = baseline(tmp_path)
    assert_matches_baseline({'always'}, path, platform='linux')
    assert_matches_baseline({'always', 'mac-only'}, path, platform='darwin')


def test_a_platform_tag_still_fails_when_that_platform_drifts(tmp_path):
    path = baseline(tmp_path)
    with pytest.raises(AssertionError, match='FIXED'):
        assert_matches_baseline({'always'}, path, platform='darwin')
    with pytest.raises(AssertionError, match='NEW'):
        assert_matches_baseline({'always', 'mac-only'}, path, platform='linux')


def test_a_platform_tag_without_a_reason_is_rejected(tmp_path):
    path = tmp_path / 'baseline.txt'
    path.write_text('mac-only # platform=darwin\n', encoding='utf-8')
    with pytest.raises(AssertionError, match='reason'):
        assert_matches_baseline(set(), path, platform='linux')
