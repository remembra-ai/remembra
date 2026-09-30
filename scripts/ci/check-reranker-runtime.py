"""Build-time checks on the real reranker package and the default cached model."""

import math
import tempfile
from pathlib import Path

from sentence_transformers import CrossEncoder
from sentence_transformers.util.misc import import_module_class


def main() -> None:
    # CVE-2026-68770: a local directory must not bypass explicit code trust.
    # This harmless sentinel would be written if custom model code executed.
    with tempfile.TemporaryDirectory(prefix="remembra-model-trust-") as directory:
        root = Path(directory)
        (root / "modeling_probe.py").write_text(
            "from pathlib import Path\nPath(__file__).with_name('executed').write_text('untrusted code ran')\nclass Probe: pass\n"
        )
        try:
            import_module_class("modeling_probe.Probe", str(root), trust_remote_code=False)
        except ValueError:
            pass
        else:
            raise RuntimeError("the reranker accepted custom model code without explicit trust")
        if (root / "executed").exists():
            raise RuntimeError("untrusted custom model code executed before rejection")

    # Cache the actual shipped model and exercise its predict API, not a mock.
    model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2", trust_remote_code=False)
    scores = model.predict([("Which planet has rings?", "Saturn has rings.")])
    if len(scores) != 1 or not math.isfinite(float(scores[0])):
        raise RuntimeError("default reranker did not produce one finite score")
    print("reranker runtime: custom code rejected; default model inference passed")


if __name__ == "__main__":
    main()
