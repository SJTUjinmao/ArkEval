from enum import Enum
from pathlib import Path
from typing import TypedDict

# Constants - Evaluation Log Directories
BASE_IMAGE_BUILD_DIR = Path("image_build_logs/base")
ENV_IMAGE_BUILD_DIR = Path("image_build_logs/env")
INSTANCE_IMAGE_BUILD_DIR = Path("image_build_logs/instances")
RUN_INSTANCE_LOG_DIR = Path("run_instance_logs")
REPO_DIR = Path("repos")

# Constants - Task Instance Class
class SWEbenchInstance(TypedDict):
    repo: str
    instance_id: str
    base_commit: str
    patch: str
    test_patch: str
    problem_statement: str
    hints_text: str
    created_at: str
    version: str
    FAIL_TO_PASS: str
    PASS_TO_PASS: str
    environment_setup_commit: str

# Constants - Test Types, Statuses, Commands
FAIL_TO_PASS = "FAIL_TO_PASS"
FAIL_TO_FAIL = "FAIL_TO_FAIL"
PASS_TO_PASS = "PASS_TO_PASS"
PASS_TO_FAIL = "PASS_TO_FAIL"

class ResolvedStatus(Enum):
    NO = "RESOLVED_NO"
    PARTIAL = "RESOLVED_PARTIAL"
    FULL = "RESOLVED_FULL"

class TestStatus(Enum):
    FAILED = "FAILED"
    PASSED = "PASSED"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"

SPECS_JADX = {
    k: {
        "root_path": "",
        "jdk_version": "8",
        "env_type": "gradle",
        "eval_cmd": "./gradlew {module}:{test_spec} --tests {test_func} >{module}:{test_func}.test.log 2>&1",
        'pre-install': [
            "sed -i '$a *.class' .gitignore"
        ]
    }
    for k in ["0.1"]
}

SPECS_DUBBO = {
    k: {
        "root_path": "",
        "jdk_version": "17",
        "env_type": "maven",
        "eval_cmd": "(mvn clean install -Dmaven.test.skip=true ; mvn clean test -Dsurefire.useFile=false -Dmaven.test.skip=false -D{test_spec}={test_func} -DfailIfNoTests=false -pl {module}) >>{module_escaped}:{test_func}.test.log"
    }
    for k in ["0.1"]
}

SPECS_COMMONS_LANG = {
    k: {
        "root_path": "",
        "jdk_version": "11",
        "env_type": "maven",
    }
    for k in ["0.1"]
}

SPECS_RXJAVA = {
    k: {
        "root_path": "",
        "jdk_version": "8",
        "env_type": "gradle",
    }
    for k in ["0.1"]
}

SPECS_JIB = {
    k: {
        "root_path": "",
        "jdk_version": "11",
        "env_type": "gradle",
        "eval_cmd": "./gradlew {module}:{test_spec} --tests {test_func} >{module}:{test_func}.test.log 2>&1"
    }
    for k in ["0.1"]
}


SPECS_EUREKA = {
    k: {
        "root_path": "",
        "jdk_version": "8",
        "env_type": "gradle",
        "eval_cmd": "./gradlew {module}:{test_spec} --tests {test_func} >{module}:{test_func}.test.log 2>&1"
    }
    for k in ["0.1"]
}

SPECS_CAMEL = {
    k: {
        "root_path": "",
        "jdk_version": "11",
        "env_type": "maven",
    }
    for k in ["0.1"]
}


SPECS_MOCKITO = {
    k: {
        "root_path": "",
        "jdk_version": "11",
        "env_type": "gradle",
    }
    for k in ["0.1"]
}

SPECS_GSON = {
    k: {
        "root_path": "",
        "jdk_version": "11",
        "env_type": "maven",
        'pre-install': [
            "sed -i '$a *.class' .gitignore"
        ]
    }
    for k in ["0.1"]
}

SPECS_JACKSON_CORE = {
    k: {
        "root_path": "",
        "jdk_version": v,
        "env_type": "maven",
        "pre-install": [
            "sed -i '/<artifactId>jackson-base/,/-SNAPSHOT<\/version>/s/-SNAPSHOT//' pom.xml",
            "git add pom.xml && git commit -m 'pre-message' "
        ]
    }
    for k, v in {"0.1": "8", "0.2": "11", "0.3": "17"}.items()
}

SPECS_JACKSON_DATABIND = {
    k: {
        "root_path": "",
        "jdk_version": "8",
        "env_type": "maven",
        "pre-install": [
            "sed -i '/<artifactId>jackson-base/,/-SNAPSHOT<\/version>/s/-SNAPSHOT//' pom.xml",
            "git add pom.xml && git commit -m 'pre-message' "
        ]
    }
    for k in ["0.1"]
}

SPECS_JACKSON_DATAFORMAT_XML = {
    k: {
        "root_path": "",
        "jdk_version": "8",
        "env_type": "maven",
        "pre-install": [
            "sed -i '/<artifactId>jackson-base/,/-SNAPSHOT<\/version>/s/-SNAPSHOT//' pom.xml",
            "git add pom.xml && git commit -m 'pre-message' "
        ]
    }
    for k in ["0.1"]
}


# Constants - Task Instance Instllation Environment
MAP_REPO_VERSION_TO_SPECS = {
    "skylot/jadx": SPECS_JADX,
    "apache/dubbo": SPECS_DUBBO,
    "apache/commons-lang": SPECS_COMMONS_LANG,
    "reactivex/rxjava": SPECS_RXJAVA,
    "googlecontainertools/jib": SPECS_JIB,
    "netflix/eureka": SPECS_EUREKA,
    "apache/camel": SPECS_CAMEL,
    "mockito/mockito": SPECS_MOCKITO,
    "google/gson": SPECS_GSON,
    "fasterxml/jackson-core": SPECS_JACKSON_CORE,
    "fasterxml/jackson-databind": SPECS_JACKSON_DATABIND,
    "fasterxml/jackson-dataformat-xml": SPECS_JACKSON_DATAFORMAT_XML,
}

# Constants - Evaluation Keys
KEY_INSTANCE_ID = "instance_id"
KEY_MODEL = "model_name_or_path"
KEY_PREDICTION = "model_patch"


# Constants - Logging
APPLY_PATCH_FAIL = ">>>>> Patch Apply Failed"
APPLY_PATCH_PASS = ">>>>> Applied Patch"
INSTALL_FAIL = ">>>>> Init Failed"
INSTALL_PASS = ">>>>> Init Succeeded"
INSTALL_TIMEOUT = ">>>>> Init Timed Out"
RESET_FAILED = ">>>>> Reset Failed"
TESTS_ERROR = ">>>>> Tests Errored"
TESTS_FAILED = ">>>>> Some Tests Failed"
TESTS_PASSED = ">>>>> All Tests Passed"
TESTS_TIMEOUT = ">>>>> Tests Timed Out"


# Constants - Patch Types
class PatchType(Enum):
    PATCH_GOLD = "gold"
    PATCH_PRED = "pred"
    PATCH_PRED_TRY = "pred_try"
    PATCH_PRED_MINIMAL = "pred_minimal"
    PATCH_PRED_MINIMAL_TRY = "pred_minimal_try"
    PATCH_TEST = "test"

    def __str__(self):
        return self.value


# Constants - Miscellaneous
NON_TEST_EXTS = [
    ".json",
    ".png",
    "csv",
    ".txt",
    ".md",
    ".jpg",
    ".jpeg",
    ".pkl",
    ".yml",
    ".yaml",
    ".toml",
]
SWE_BENCH_URL_RAW = "https://raw.githubusercontent.com/"