"""pytest entry for the offscreen feature tests (runs them in a subprocess)."""
import os, subprocess, sys

def test_features_offscreen():
    here = os.path.dirname(os.path.abspath(__file__))
    r = subprocess.run([sys.executable, os.path.join(here, 'test_features_offscreen.py')], capture_output=True, text=True,
                       env=dict(os.environ, QT_QPA_PLATFORM='offscreen'))
    assert r.returncode == 0, '\n'.join(l for l in r.stdout.splitlines() if l.startswith('FAIL')) or r.stderr[-2000:]
