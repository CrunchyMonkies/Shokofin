from datetime import datetime
import os
import json
import yaml
import argparse
import re

def extract_target_framework(csproj_path):
    with open(csproj_path, "r") as file:
        content = file.read()
    target_framework_match = re.compile(r"<TargetFramework>(.*?)<\/TargetFramework>", re.IGNORECASE).search(content)
    target_frameworks_match = re.compile(r"<TargetFrameworks>(.*?)<\/TargetFrameworks>", re.IGNORECASE).search(content)
    if target_framework_match:
        return [target_framework_match.group(1)]
    elif target_frameworks_match:
        return target_frameworks_match.group(1).split(";")
    else:
        return None

def extract_packages_to_output(csproj_path, framework):
    with open(csproj_path, "r") as file:
        content = file.read()
    pattern = re.compile(
        rf'<CommonPackageReference\s+Include="([^"]+)"\s+'
        rf'Version="[^"]+"\s+'
        rf'TargetFramework="{re.escape(framework)}"\s*/>'
    )
    matches = [match.group(1) + ".dll" for match in pattern.finditer(content)]
    return list(set(matches))

# Fallback ABI for frameworks whose Jellyfin.Controller version cannot be read as a plain
# number from the csproj (e.g. the net10.0 / Jellyfin v12 target references a locally-built
# assembly or a pre-release package like "12.0.0-rc2").
FRAMEWORK_ABI = {
    "net10.0": "12.0.0",
}

def extract_target_abi(csproj_path, framework):
    with open(csproj_path, "r") as file:
        content = file.read()
    pattern = re.compile(
        rf'<PackageReference\s+Include="Jellyfin\.Controller"\s+'
        rf'Version="([^"]+)"\s+'
        rf'TargetFramework="{re.escape(framework)}"\s*/>',
        re.IGNORECASE,
    )
    match = pattern.search(content)
    version = match.group(1) if match else None

    # Keep only a clean numeric version (strip pre-release suffixes and MSBuild $(props)),
    # e.g. "12.0.0-rc2" -> "12.0.0". Fall back to the per-framework ABI map when the version
    # isn't numeric (property placeholder) or has no PackageReference (local assembly ref).
    numeric = re.match(r"\d+(?:\.\d+)*", version) if version else None
    if numeric:
        return numeric.group(0)
    if framework in FRAMEWORK_ABI:
        return FRAMEWORK_ABI[framework]
    raise Exception(
        f"Jellyfin.Controller not found for framework '{framework}' in {os.path.basename(csproj_path)}"
    )

parser = argparse.ArgumentParser()
parser.add_argument("--repo", required=True)
parser.add_argument("--version", required=True)
parser.add_argument("--tag", required=True)
parser.add_argument("--prerelease", default=False)
# Restrict the build to specific target framework(s) (comma-separated). When omitted, build
# every framework EXCEPT net10.0, so the legacy stable/dev workflows keep producing the
# 10.x artifacts and the net10.0 / Jellyfin v12 build is opt-in via the dedicated workflow.
parser.add_argument("--framework", default=None)
opts = parser.parse_args()

if opts.framework:
    selected_frameworks = [f.strip() for f in opts.framework.split(",") if f.strip()]
else:
    selected_frameworks = None
# Frameworks skipped by default (only built when explicitly requested via --framework).
DEFAULT_SKIP_FRAMEWORKS = {"net10.0"}

project_file = "./Shokofin/Shokofin.csproj"
version = opts.version
tag = opts.tag
prerelease = bool(opts.prerelease)
short_version = ".".join(version.split(".")[:3])
build_number = int(version.split(".")[-1])

artifact_dir = os.path.join(os.getcwd(), "artifacts")
if not os.path.exists(artifact_dir):
    os.mkdir(artifact_dir)

jellyfin_repo_file="./manifest.json"
jellyfin_repo_url=f"https://github.com/{opts.repo}/releases/download"

# Load the build.yaml file into memory.
build_file = "./build.yaml"
with open(build_file, "r") as file:
    build_file_contents = file.read()
    data = yaml.safe_load(build_file_contents)

# Add changelog to the build yaml before we generate the release.
if "changelog" in data:
    if "CHANGELOG" in os.environ:
        data["changelog"] = os.environ["CHANGELOG"].strip()
    else:
        data["changelog"] = ""
changelog = data["changelog"]

# For every found framework, generate a zip file for the target framework and ABI.
try:
    for framework in extract_target_framework(project_file):
        if selected_frameworks is not None:
            if framework not in selected_frameworks:
                continue
        elif framework in DEFAULT_SKIP_FRAMEWORKS:
            continue
        target_abi = extract_target_abi(project_file, framework)
        target_abi_high = ".".join(target_abi.split(".")[:-1])
        target_abi_low = target_abi.split(".")[1]
        artifacts = extract_packages_to_output(project_file, framework)

        if build_number != "0":
            generated_version = f"{short_version}.{build_number}{target_abi_low}"
        else:
            generated_version = f"{short_version}.{target_abi_low}"
        generated_changelog = f"Only compatible with **{target_abi_high}.z**.\n\nSee the [release notes](https://github.com/ShokoAnime/Shokofin/releases/tag/{tag}) for more info."
        if changelog:
            generated_changelog += f"\n\n---\n\n{changelog}"

        data = yaml.safe_load(build_file_contents)
        data["changelog"] = generated_changelog
        data["artifacts"] = list(set(data["artifacts"] + artifacts))
        data["targetAbi"] = target_abi + ".0"
        with open(build_file, "w") as file:
            yaml.dump(data, file, sort_keys=False)

        zipfile=os.popen("jprm --verbosity=debug plugin build \".\" --output=\"%s\" --version=\"%s\" --dotnet-framework=\"%s\"" % (artifact_dir, generated_version, framework)).read().strip()

        # read the checksum file jprm wrote
        checksum = open(zipfile + ".md5sum", "r").read().strip()[:32]
        timestamp = os.path.getmtime(zipfile)
        new_zipfile = os.path.join(artifact_dir, f"shoko_{version}_for_{target_abi_high}.zip")
        os.rename(zipfile, new_zipfile)
        os.remove(zipfile + ".md5sum")
        os.remove(zipfile + ".meta.json")

        jellyfin_plugin_release_url=f"{jellyfin_repo_url}/{tag}/shoko_{version}_for_{target_abi_high}.zip"
        os.system("jprm repo add --plugin-url=%s %s %s" % (jellyfin_plugin_release_url, jellyfin_repo_file, new_zipfile))
finally:
    # Restore the original build.yaml after we're done
    with open(build_file, "w") as file:
        file.write(build_file_contents)

# Compact the unstable manifest after building, so it only contains the last 10 versions.
if prerelease:
    with open(jellyfin_repo_file, "r") as file:
        repos = json.load(file)
        repo = repos[0]
    if "versions" in repo and len(repo["versions"]) > 10:
        repo["versions"] = repo["versions"][:10]

    # Update the repository file
    with open(jellyfin_repo_file, "w") as file:
        json.dump(repos, file, indent=4)

print(version)
