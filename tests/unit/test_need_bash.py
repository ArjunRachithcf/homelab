"""lib/need-bash.sh: the entry points load it first, and it stays bash 3.2 syntax (macOS's /bin/bash)."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "lib/need-bash.sh"
# Bash 4+ only: mapfile/readarray, associative or nameref declarations, case changes, ${a[-1]}, |&, &>>,
# ;& / ;;& in case, coproc, ${x@Q}
BASH4 = re.compile(r"\bmapfile\b|\breadarray\b|declare -[a-zA-Z]*[An]|local -[a-zA-Z]*[An]|\$\{[^}]*(\^\^?|,,?)\}"
                   r"|\[-[0-9]+\]\}|\|&|&>>|;;?&|\bcoproc\b|\$\{[^}]*@[QEPAa]\}")


class NeedBash(unittest.TestCase):
    def test_entry_points_load_it_first(self):
        # Before anything else runs (and before any cd: it re-runs "$0")
        for f, first in (("homelab", 'source "$here/lib/need-bash.sh"'),
                         ("lib/platform/launchd-run.sh", 'source "$(dirname "$0")/../need-bash.sh"'),
                         ("lib/wizard.sh", 'source "$(dirname "${BASH_SOURCE[0]}")/need-bash.sh"')):
            src = (ROOT / f).read_text()
            self.assertIn(first, src, f)
            before = src[:src.index(first)]
            self.assertNotRegex(before, r"(?m)^cd ", f)

    def test_every_wizard_sources_the_library_before_cd(self):
        for w in sorted(ROOT.glob("*wizard.sh")):
            src = w.read_text()
            i = src.index("lib/wizard.sh\"")
            self.assertNotRegex(src[:i], r"(?m)^cd ", w.name)

    def test_guard_is_bash32(self):
        # Without comments (whole-line and trailing)
        code = "\n".join(re.sub(r"\s+#\s.*$", "", l) for l in GUARD.read_text().splitlines()
                          if not l.lstrip().startswith("#"))
        self.assertIsNone(BASH4.search(code))
        # An empty "$@" is "unbound" in bash 3.2 under set -u
        self.assertNotRegex(code, r'(?<!\{1\+)"\$@"(?!\})')


if __name__ == "__main__":
    unittest.main()
