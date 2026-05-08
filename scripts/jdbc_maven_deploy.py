# https://central.sonatype.org/publish/publish-portal-api/

# this is the pgp key we use to sign releases
# if this key should be lost, generate a new one with `gpg --full-generate-key`
# AND upload to keyserver: `gpg --keyserver hkp://keys.openpgp.org --send-keys [...]`
# export the keys for GitHub Actions like so: `gpg --export-secret-keys | base64`
# --------------------------------
# pub   ed25519 2022-02-07 [SC]
#       65F91213E069629F406F7CF27F610913E3A6F526
# uid           [ultimate] DuckDB <quack@duckdb.org>
# sub   cv25519 2022-02-07 [E]

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
import re
import base64
import hashlib
from os import path


def exec(cmd):
    print(cmd)
    res = subprocess.run(cmd.split(' '), capture_output=True)
    if res.returncode == 0:
        return res.stdout
    raise ValueError(res.stdout + res.stderr)


if len(sys.argv) < 4 or not os.path.isdir(sys.argv[2]) or not os.path.isdir(sys.argv[3]):
    print("Usage: [release_tag, format: v1.2.3.4] [artifact_dir] [jdbc_root_path]")
    exit(1)

version_regex = re.compile(r'^v((\d+)\.(\d+)\.\d+\.\d+(?:-[\w.-]+)?)$')
release_tag = sys.argv[1]
deploy_url = 'https://central.sonatype.com/api/v1/publisher/upload'
is_release = True

if version_regex.match(release_tag):
    release_version = version_regex.search(release_tag).group(1)
else:
    # SNAPSHOT build (main or any non-tag ref).
    # Version resolution order:
    #   1. SNAPSHOT_VERSION env var (explicit override, used as-is)
    #   2. latest git tag matching the version regex -> bump minor
    #   3. fallback to "0.0.0.0-SNAPSHOT" so the build still produces a
    #      bundle even on a fork without any release tags.
    release_version = None
    override = os.environ.get("SNAPSHOT_VERSION")
    if override:
        release_version = override
    else:
        last_tag = exec('git tag --sort=-committerdate').decode('utf8').split('\n')[0].strip()
        if last_tag:
            re_result = version_regex.search(last_tag)
            if re_result is None:
                print("Could not parse last tag %s, falling back to default" % last_tag)
            else:
                release_version = "%d.%d.0.0-SNAPSHOT" % (
                    int(re_result.group(2)),
                    int(re_result.group(3)) + 1,
                )
        else:
            print("No git tags found, falling back to default SNAPSHOT version")
    if release_version is None:
        release_version = "0.0.0.0-SNAPSHOT"
    is_release = False

jdbc_artifact_dir = sys.argv[2]
jdbc_root_path = sys.argv[3]

combine_builds = ['linux-amd64', 'osx-universal', 'windows-amd64', 'linux-aarch64']
arch_specific_builds = [
  'linux-amd64',
  'linux-aarch64',
  'linux-amd64-musl',
  'linux-aarch64-musl',
  'osx-universal',
  'windows-amd64',
  'windows-aarch64',
]
arch_specific_classifiers = [
  'linux_amd64',
  'linux_arm64',
  'linux_amd64_musl',
  'linux_arm64_musl',
  'macos_universal',
  'windows_amd64',
  'windows_arm64',
]

staging_dir = tempfile.mkdtemp()

binary_jar = '%s/duckdb_jdbc-%s.jar' % (staging_dir, release_version)
pom = '%s/duckdb_jdbc-%s.pom' % (staging_dir, release_version)
sources_jar = '%s/duckdb_jdbc-%s-sources.jar' % (staging_dir, release_version)
javadoc_jar = '%s/duckdb_jdbc-%s-javadoc.jar' % (staging_dir, release_version)
nolib_jar = '%s/duckdb_jdbc-%s-nolib.jar' % (staging_dir, release_version)

arch_specific_jars = []
for i in range(len(arch_specific_builds)):
  build = arch_specific_builds[i]
  classifier = arch_specific_classifiers[i]
  arch_specific_jars.append('%s/duckdb_jdbc-%s-%s.jar' % (staging_dir, release_version, classifier))

pom_template = """
<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>org.duckdb</groupId>
  <artifactId>duckdb_jdbc</artifactId>
  <version>${VERSION}</version>
  <packaging>jar</packaging>
  <name>DuckDB JDBC Driver</name>
  <description>A JDBC-Compliant driver for the DuckDB data management system</description>
  <url>https://www.duckdb.org</url>

  <licenses>
    <license>
      <name>MIT License</name>
      <url>https://raw.githubusercontent.com/duckdb/duckdb/main/LICENSE</url>
      <distribution>repo</distribution>
    </license>
  </licenses>

  <developers>
      <developer>
      <name>Mark Raasveldt</name>
      <email>mark@duckdblabs.com</email>
      <organization>DuckDB Labs</organization>
      <organizationUrl>https://www.duckdblabs.com</organizationUrl>
    </developer>
    <developer>
      <name>Hannes Muehleisen</name>
      <email>hannes@duckdblabs.com</email>
      <organization>DuckDB Labs</organization>
      <organizationUrl>https://www.duckdblabs.com</organizationUrl>
    </developer>
  </developers>

  <scm>
    <connection>scm:git:git://github.com/duckdb/duckdb.git</connection>
    <developerConnection>scm:git:ssh://github.com:duckdb/duckdb.git</developerConnection>
    <url>http://github.com/duckdb/duckdb/tree/main</url>
  </scm>

</project>
<!-- Note: this cannot be used to build the JDBC driver, we only use it to deploy -->
"""

# create a matching POM with this version
pom_path = pathlib.Path(pom)
pom_path.write_text(pom_template.replace("${VERSION}", release_version))

# prepare 'empty' jar
linux_amd64_src_jar = os.path.join(jdbc_artifact_dir, "java-" + combine_builds[0], "duckdb_jdbc.jar")
with zipfile.ZipFile(linux_amd64_src_jar) as linux_amd64:
  with zipfile.ZipFile(binary_jar, mode='w') as nolib:
    for item in linux_amd64.infolist():
      if item.filename != "libduckdb_java.so_linux_amd64":
        buffer = linux_amd64.read(item.filename)
        nolib.writestr(item, buffer)

# copy 'empty' jar to '-nolib' classifier
shutil.copyfile(binary_jar, nolib_jar)

# fatten up 'empty' jar adding native libs
for build in combine_builds:
    old_jar = zipfile.ZipFile(os.path.join(jdbc_artifact_dir, "java-" + build, "duckdb_jdbc.jar"), 'r')
    for zip_entry in old_jar.namelist():
        if zip_entry.startswith('libduckdb_java.so'):
            old_jar.extract(zip_entry, staging_dir)
            exec("jar -uf %s -C %s %s" % (binary_jar, staging_dir, zip_entry))

javadoc_stage_dir = tempfile.mkdtemp()

exec("javadoc -Xdoclint:-reference -d %s -sourcepath %s/src/main/java org.duckdb" % (javadoc_stage_dir, jdbc_root_path))
exec("jar -cvf %s -C %s ." % (javadoc_jar, javadoc_stage_dir))
exec("jar -cvf %s -C %s/src/main/java org" % (sources_jar, jdbc_root_path))

# copy arch-specific JARs
for i in range(len(arch_specific_builds)):
  build = arch_specific_builds[i]
  src_jar = os.path.join(jdbc_artifact_dir, "java-" + build, "duckdb_jdbc.jar")
  dest_jar = arch_specific_jars[i] 
  shutil.copyfile(src_jar, dest_jar)

files_to_deploy = [
  binary_jar,
  sources_jar,
  javadoc_jar,
  nolib_jar,
  pom
]
for jar in arch_specific_jars:
  files_to_deploy.append(jar)

# make sure all files exist before continuing
for file in files_to_deploy:
  if not path.isfile(file):
    raise ValueError(f"Could not create all required files: {file}")

# now sign and upload everything
# for this to work, you must have MAVEN_USERNAME and MAVEN_PASSWORD
# environment variables for the Sonatype Central Portal

bundle_root_dir = path.join(staging_dir, "central-bundle")
bundle_zip = path.join(staging_dir, "central-bundle.zip")

bundle_dir = path.join(bundle_root_dir, "org", "duckdb", "duckdb_jdbc", release_version)
os.makedirs(bundle_dir)

for file in files_to_deploy:
  file_name = path.basename(file)
  bundle_file = path.join(bundle_dir, file_name)
  shutil.copyfile(file, bundle_file)
  subprocess.run(["gpg", "--sign", "-ab", file_name], cwd=bundle_dir)
  with open(bundle_file, "rb") as fd:
    file_bytes = fd.read()
  for alg in ["md5", "sha1", "sha256"]:
    digest = hashlib.new(alg)
    digest.update(file_bytes)
    hashsum = digest.hexdigest()
    with open(f"{bundle_file}.{alg}", "w") as fd:
      fd.write(hashsum)

subprocess.run(["ls", "-laR", bundle_root_dir])
subprocess.run(["zip", "-qr", bundle_zip, "org"], cwd=bundle_root_dir)

# copy bundle zip to current working directory so it can be picked up as a
# GitHub Actions artifact by the maven-deploy workflow job
bundle_zip_output = path.join(os.getcwd(), "central-bundle.zip")
shutil.copyfile(bundle_zip, bundle_zip_output)
print("Bundle zip copied to: %s" % bundle_zip_output)

# ---------------------------------------------------------------------------
# Deploy to JFrog Artifactory
# ---------------------------------------------------------------------------
# The bundle_dir already contains every file in a Maven repository layout
# (jars, pom, .asc signatures, .md5/.sha1/.sha256 checksums) under
# org/duckdb/duckdb_jdbc/<version>/. Artifactory natively accepts HTTP PUT
# uploads against that same path, so we don't need Maven installed.
#
# Auth is taken from ARTIFACTORY_CREDENTIALS_USR / ARTIFACTORY_CREDENTIALS_PSW
# (matches Tealium's internal convention). If the variables are not set the
# deployment step is skipped, which lets the script still run for local
# builds or PR jobs without credentials.
artifactory_user = os.environ.get("ARTIFACTORY_CREDENTIALS_USR")
artifactory_password = os.environ.get("ARTIFACTORY_CREDENTIALS_PSW")
artifactory_base_url = os.environ.get(
    "ARTIFACTORY_BASE_URL", "https://tealium.jfrog.io/artifactory"
)
artifactory_release_repo = os.environ.get(
    "ARTIFACTORY_RELEASE_REPO", "maven-ext-local-release"
)
artifactory_snapshot_repo = os.environ.get(
    "ARTIFACTORY_SNAPSHOT_REPO", "maven-ext-local-snapshot"
)

if artifactory_user and artifactory_password:
    target_repo = artifactory_release_repo if is_release else artifactory_snapshot_repo
    artifact_path = "org/duckdb/duckdb_jdbc/%s" % release_version
    upload_base_url = "%s/%s/%s" % (
        artifactory_base_url.rstrip("/"),
        target_repo,
        artifact_path,
    )
    print("Deploying to Artifactory: %s" % upload_base_url)

    for file_name in sorted(os.listdir(bundle_dir)):
        src_file = path.join(bundle_dir, file_name)
        if not path.isfile(src_file):
            continue
        upload_url = "%s/%s" % (upload_base_url, file_name)
        print("  uploading %s" % file_name)
        subprocess.run(
            [
                "curl",
                # do NOT enable --verbose on CI, it leaks the auth token
                "--silent",
                "--show-error",
                "--fail",
                "--user",
                "%s:%s" % (artifactory_user, artifactory_password),
                "--upload-file",
                src_file,
                upload_url,
            ],
            check=True,
        )
    print("Artifactory deploy complete (version=%s, repo=%s)" % (release_version, target_repo))
else:
    print(
        "ARTIFACTORY_CREDENTIALS_USR / ARTIFACTORY_CREDENTIALS_PSW not set; "
        "skipping Artifactory deploy"
    )

# ---------------------------------------------------------------------------
# Sonatype Central Portal upload (disabled)
# ---------------------------------------------------------------------------
# for this to work, you must have MAVEN_USERNAME and MAVEN_PASSWORD
# environment variables for the Sonatype Central Portal

# maven_username = os.environ["MAVEN_USERNAME"]
# maven_password = os.environ["MAVEN_PASSWORD"]
# token = base64.b64encode(f"{maven_username}:{maven_password}".encode("utf-8")).decode("utf-8")

# subprocess.run([
#   "curl",
#   # "--verbose", do NOT enable it on CI, it leaks the auth token
#   "--silent",
#   "--header", f"Authorization: Bearer {token}",
#   "--form", f"name={release_version}",
#   "--form", "publishingType=AUTOMATIC",
#   "--form", f"bundle=@{bundle_zip}",
#   deploy_url,
#   ], cwd=bundle_root_dir, check=True)

print("Done?")
