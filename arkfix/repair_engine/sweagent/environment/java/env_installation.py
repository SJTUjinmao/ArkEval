from sweagent.environment.java.constants import MAP_REPO_VERSION_TO_SPECS
from sweagent.environment.swe_env import SWEEnv

def install_jdk(jdk_version, env: SWEEnv):
    exist_version = env.communicate("java -version")
    reqs_commands = ['cd /']
    if 'version' in exist_version:
        exist_version = exist_version.split('"')[1].split('"')[0]
        main_version = exist_version.split('.')[0]
        if main_version == '1':
            main_version = exist_version.split('.')[1]
        if int(main_version) == jdk_version:
            return []
        else:
            reqs_commands += [
                "apt remove --purge -y openjdk-*",
                "apt autoremove -y",
            ]
    reqs_commands += [
            "apt-get update --allow-insecure-repositories",
            f"apt-get install -y openjdk-{jdk_version}-jdk --allow-unauthenticated",
            "java -version",
        ]
    return reqs_commands


def repo_install_commands(env: SWEEnv, instance, repo_directory):
    # first change source of maven
    specs = MAP_REPO_VERSION_TO_SPECS[instance['repo']][instance['version']]
    root_path = specs.get("root_path", "")
    env_type = specs.get("env_type", "")
    jdk_version = specs['jdk_version']
    setup_commands = install_jdk(jdk_version, env)
    build_cmds = []
    if env_type == "maven":
        build_cmds.extend([
            "export M2_HOME=/opt/maven",
            "export MAVEN_HOME=/opt/maven",
            "export PATH=${M2_HOME}/bin:${PATH}",
            "mvn -version",
        ])
        build_cmds += pre_install_commands(instance)
        build_cmds.append("mvn clean install -Dmaven.test.skip=true")
    elif env_type == "gradle":
        # change gradle source
        build_cmds += pre_install_commands(instance)
        build_cmds.append("./gradlew dependencies;  echo ///PROCESS-DONE:$?:PROCESS-DONE///")

    setup_commands += [
        f"cd {repo_directory}/{root_path}",
        *build_cmds,
    ]
    return setup_commands

def pre_install_commands(instance):
    repo = instance['repo']
    version = instance['version']
    specs = MAP_REPO_VERSION_TO_SPECS[repo][version]
    if specs.get('pre-install'):
        return [f'git reset --hard {instance["base_commit"]}'] + specs['pre-install']
    return []
