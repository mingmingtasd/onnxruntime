#!/usr/bin/env python3

# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

import argparse
import pathlib
import sys


_script_dir = pathlib.Path(__file__).parent.resolve(strict=True)
sys.path.append(str(_script_dir.parent))


from package_assembly_utils import (  # noqa: E402
    copy_repo_relative_to_dir, gen_file_from_template, load_framework_info)


# these variables contain paths or path patterns that are relative to the repo root

# the license file
license_file = "LICENSE"

# include directories for compiling the pod itself
include_dirs = [
    "objectivec",
    "cmake/external/SafeInt",
]

# pod source files
source_files = [
    "objectivec/include/*.h",
    "objectivec/src/*.h",
    "objectivec/src/*.m",
    "objectivec/src/*.mm",
    "cmake/external/SafeInt/safeint/SafeInt.hpp",
]

# pod public header files
# note: these are a subset of source_files
public_header_files = [
    "objectivec/include/*.h",
]

# pod test source files
test_source_files = [
    "objectivec/test/*.h",
    "objectivec/test/*.m",
    "objectivec/test/*.mm",
]

# pod test resource files
test_resource_files = [
    "objectivec/test/testdata/*.ort",
]


def parse_args():
    parser = argparse.ArgumentParser(description="""
        Assembles the files for the Objective-C pod package in a staging directory.
        This directory can be validated (e.g., with `pod lib lint`) and then zipped to create a package for release.
    """)

    parser.add_argument("--staging-dir", type=pathlib.Path,
                        default=pathlib.Path("./onnxruntime-mobile-objc-staging"),
                        help="Path to the staging directory for the Objective-C pod files.")
    parser.add_argument("--pod-version", required=True,
                        help="Objective-C pod version.")
    parser.add_argument("--framework-info-file", type=pathlib.Path, required=True,
                        help="Path to the framework_info.json file containing additional values for the podspec. "
                             "This file should be generated by CMake in the build directory.")

    return parser.parse_args()


def main():
    args = parse_args()

    framework_info = load_framework_info(args.framework_info_file.resolve())

    staging_dir = args.staging_dir.resolve()
    print(f"Assembling files in staging directory: {staging_dir}")
    if staging_dir.exists():
        print("Warning: staging directory already exists", file=sys.stderr)

    # copy the necessary files to the staging directory
    copy_repo_relative_to_dir(
        [license_file] + source_files + test_source_files + test_resource_files,
        staging_dir)

    # generate the podspec file from the template

    def path_patterns_as_variable_value(patterns: list[str]):
        return ", ".join([f'"{pattern}"' for pattern in patterns])

    variable_substitutions = {
        "VERSION": args.pod_version,
        "IOS_DEPLOYMENT_TARGET": framework_info["IOS_DEPLOYMENT_TARGET"],
        "LICENSE_FILE": path_patterns_as_variable_value([license_file]),
        "INCLUDE_DIR_LIST": path_patterns_as_variable_value(include_dirs),
        "PUBLIC_HEADER_FILE_LIST": path_patterns_as_variable_value(public_header_files),
        "SOURCE_FILE_LIST": path_patterns_as_variable_value(source_files),
        "TEST_SOURCE_FILE_LIST": path_patterns_as_variable_value(test_source_files),
        "TEST_RESOURCE_FILE_LIST": path_patterns_as_variable_value(test_resource_files),
    }

    podspec_template = _script_dir / "onnxruntime-mobile-objc.podspec.template"
    podspec = staging_dir / "onnxruntime-mobile-objc.podspec"

    gen_file_from_template(podspec_template, podspec, variable_substitutions)

    return 0


if __name__ == "__main__":
    sys.exit(main())
