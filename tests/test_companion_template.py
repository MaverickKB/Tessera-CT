"""The shipped companion template stays generic, and the release names its gaps."""

import json
from pathlib import Path

from tessera.config import Config
from release_privacy import copyright_notice, privacy_hits


ROOT = Path(__file__).resolve().parents[1]


def test_template_companion_has_no_personal_history_and_is_not_loaded():
    directory = ROOT / 'examples' / 'companion'
    names = sorted(path.name for path in directory.iterdir())
    assert names == ['CORTEX.md', 'README.md', 'SOUL.md']
    assert privacy_hits(directory) == []
    assert 'identity_file' in (directory / 'README.md').read_text(encoding='utf-8')
    home = ROOT / 'examples' / 'companion'
    config = Config(home=home, cwd=home)
    assert config.identity_file == ''
    assert config.identity_files() == []
    patterns = [line.strip() for line in (ROOT / 'release' / 'exclude.txt').read_text(encoding='utf-8').splitlines()
                if line.strip() and not line.strip().startswith('#')]
    assert not any('companion' in pattern or pattern in {'SOUL.md', 'CORTEX.md'} for pattern in patterns)


def test_release_copy_states_native_icon_and_custom_skin_gaps():
    brand = (ROOT / 'docs' / 'brand' / 'README.md').read_text(encoding='utf-8')
    config = (ROOT / 'docs' / 'reference' / 'CONFIG.md').read_text(encoding='utf-8')
    for text in (brand, config):
        assert 'dock' in text
        assert 'alternate' in text
        assert 'future update' in text
        assert 'gremlin' in text
    notice = (ROOT / 'NOTICE').read_text(encoding='utf-8').splitlines()[0]
    assert notice == copyright_notice()
    assert copyright_notice() in (ROOT / 'LICENSE').read_text(encoding='utf-8')
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    assert copyright_notice() in readme and 'NOTICE' in readme
    swift = '\n'.join(path.read_text(encoding='utf-8')
                      for path in (ROOT / 'examples' / 'ios').rglob('*.swift'))
    assert 'Tessera-CT' in swift
    assert 'setAlternateIconName' not in swift
    assets = ROOT / 'examples/ios/TesseraVoice/TesseraVoice/Resources/Assets.xcassets'
    assert sorted(path.name for path in assets.iterdir()) == [
        'AccentColor.colorset', 'AppIcon.appiconset', 'BrandMark.imageset', 'Contents.json']
    drawings = [path.relative_to(ROOT).as_posix() for path in ROOT.rglob('f-it-gremlin.svg')]
    assert drawings == ['web/brand/f-it-gremlin.svg']
    script = (ROOT / 'web/unleashed-label.js').read_text(encoding='utf-8')
    assert 'M44 50' not in script
    # The mark is the chosen goblin avatar now, and every avatar it can name
    # ships beside the page at both sizes.
    assert '/static/brand/goblin' in script
    manifest = json.loads((ROOT / 'web/brand/goblin/manifest.json').read_text(encoding='utf-8'))
    assert manifest, 'the avatar manifest names every avatar'
    for avatar in manifest:
        for size in ('64', '256'):
            picture = ROOT / 'web/brand/goblin' / avatar['id'] / ('avatar-' + size + '.png')
            assert picture.is_file(), picture
