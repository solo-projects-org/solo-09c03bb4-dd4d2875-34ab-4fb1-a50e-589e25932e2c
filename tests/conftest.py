import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="pm-test-")
os.environ["PM_DB_PATH"] = os.path.join(_tmp, "test.db")
