"""Portable, on-demand PO-token provider options for the bundled yt-dlp."""
from pathlib import Path


def provider_options(base):
    root = Path(base)
    runtime = root / 'runtime' / 'node.exe'
    script = root / 'build' / 'generate_once.js'
    plugin = root / 'plugins' / 'bgutil-ytdlp-pot-provider.zip'
    if not all(path.is_file() for path in (runtime, script, plugin)):
        raise RuntimeError('Le module d’accès automatique est incomplet. Réinstallez le dossier _pot du téléchargeur.')
    return [
        '--plugin-dirs', str(root / 'plugins'),
        '--no-js-runtimes', '--js-runtimes', 'node:' + str(runtime),
        '--extractor-args', 'youtube:player_client=mweb',
        '--extractor-args', 'youtubepot-bgutilscript:server_home=' + str(root),
    ]
