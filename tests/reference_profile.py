"""The packaged reference profile's two models, named once for the tests that assert its role assignment.

They are read from the profile by the role each plays for correctness, so no test repeats a model ID.
"""

from autoreviewer.servers import DEFAULT_MODELS

QWEN = DEFAULT_MODELS["correctness"]
DEEPSEEK = DEFAULT_MODELS["correctness.verifier"]
