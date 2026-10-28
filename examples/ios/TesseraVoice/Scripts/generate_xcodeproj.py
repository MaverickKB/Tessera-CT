#!/usr/bin/env python3
"""Generate TesseraVoice.xcodeproj from ProductManifest constants and source tree."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Runtime and build settings share the product manifest.
manifest = (ROOT / "TesseraVoice/Product/ProductManifest.swift").read_text()


def manifest_string(name: str) -> str:
    match = re.search(r"static let " + re.escape(name) + r' = "([^"]*)"', manifest)
    if match is None:
        raise ValueError("Missing manifest field: " + name)
    return match.group(1)


PRODUCT_NAME = manifest_string("productName")
TARGET_NAME = "TesseraVoice"
TEST_TARGET_NAME = TARGET_NAME + "Tests"
BUNDLE_ID = "$(PRODUCT_BUNDLE_IDENTIFIER_PREFIX).tessera.voice"
TEST_BUNDLE_ID = BUNDLE_ID + ".tests"
VERSION = manifest_string("version")
BUILD_NUMBER = manifest_string("buildNumber")
MIN_IOS = manifest_string("minimumIOSVersion")
PROJECT_NAME = TARGET_NAME


def hid() -> str:
    return uuid.uuid4().hex[:24].upper()


def collect_files(base: Path, suffix: str) -> list[Path]:
    files = []
    for path in sorted(base.rglob(f"*{suffix}")):
        if path.is_file():
            files.append(path)
    return files


def main() -> None:
    app_sources = collect_files(ROOT / "TesseraVoice", ".swift")
    test_sources = collect_files(ROOT / "TesseraVoiceTests", ".swift")
    info_plist = ROOT / "TesseraVoice" / "Resources" / "Info.plist"
    assets = ROOT / "TesseraVoice" / "Resources" / "Assets.xcassets"

    ids = {
        "project": hid(),
        "root_group": hid(),
        "app_group": hid(),
        "tests_group": hid(),
        "products_group": hid(),
        "app_target": hid(),
        "test_target": hid(),
        "app_product": hid(),
        "test_product": hid(),
        "sources_phase_app": hid(),
        "sources_phase_test": hid(),
        "resources_phase_app": hid(),
        "frameworks_phase_app": hid(),
        "frameworks_phase_test": hid(),
        "target_dep": hid(),
        "container_proxy": hid(),
        "project_config_list": hid(),
        "app_config_list": hid(),
        "test_config_list": hid(),
        "project_debug": hid(),
        "project_release": hid(),
        "signing_xcconfig": hid(),
        "app_debug": hid(),
        "app_release": hid(),
        "test_debug": hid(),
        "test_release": hid(),
        "assets_file": hid(),
        "assets_build": hid(),
        "plist_file": hid(),
    }

    file_refs = {}
    build_files = {}
    for path in app_sources + test_sources:
        file_refs[path] = hid()
        build_files[path] = hid()

    # Group hierarchy under TesseraVoice/
    subgroup_names = ["App", "Product", "UI", "Settings", "Network", "Voice", "Resources"]
    subgroup_ids = {name: hid() for name in subgroup_names}

    def rel(path: Path) -> str:
        return path.relative_to(ROOT).as_posix()

    lines: list[str] = []
    lines.append("// !$*UTF8*$!")
    lines.append("{")
    lines.append("\tarchiveVersion = 1;")
    lines.append("\tclasses = {};")
    lines.append("\tobjectVersion = 56;")
    lines.append("\tobjects = {")
    lines.append("")

    # PBXBuildFile
    lines.append("/* Begin PBXBuildFile section */")
    for path in app_sources:
        lines.append(
            f"\t\t{build_files[path]} /* {path.name} in Sources */ = {{isa = PBXBuildFile; fileRef = {file_refs[path]} /* {path.name} */; }};"
        )
    for path in test_sources:
        lines.append(
            f"\t\t{build_files[path]} /* {path.name} in Sources */ = {{isa = PBXBuildFile; fileRef = {file_refs[path]} /* {path.name} */; }};"
        )
    lines.append(
        f"\t\t{ids['assets_build']} /* Assets.xcassets in Resources */ = {{isa = PBXBuildFile; fileRef = {ids['assets_file']} /* Assets.xcassets */; }};"
    )
    lines.append("/* End PBXBuildFile section */")
    lines.append("")

    # PBXContainerItemProxy
    lines.append("/* Begin PBXContainerItemProxy section */")
    lines.append(f"\t\t{ids['container_proxy']} /* PBXContainerItemProxy */ = {{")
    lines.append("\t\t\tisa = PBXContainerItemProxy;")
    lines.append(f"\t\t\tcontainerPortal = {ids['project']} /* Project object */;")
    lines.append("\t\t\tproxyType = 1;")
    lines.append(f"\t\t\tremoteGlobalIDString = {ids['app_target']};")
    lines.append(f"\t\t\tremoteInfo = {TARGET_NAME};")
    lines.append("\t\t};")
    lines.append("/* End PBXContainerItemProxy section */")
    lines.append("")

    # PBXFileReference
    lines.append("/* Begin PBXFileReference section */")
    lines.append(
        f"\t\t{ids['app_product']} /* {TARGET_NAME}.app */ = {{isa = PBXFileReference; explicitFileType = wrapper.application; includeInIndex = 0; path = {TARGET_NAME}.app; sourceTree = BUILT_PRODUCTS_DIR; }};"
    )
    lines.append(
        f"\t\t{ids['test_product']} /* {TEST_TARGET_NAME}.xctest */ = {{isa = PBXFileReference; explicitFileType = wrapper.cfbundle; includeInIndex = 0; path = {TEST_TARGET_NAME}.xctest; sourceTree = BUILT_PRODUCTS_DIR; }};"
    )
    for path in app_sources + test_sources:
        lines.append(
            f"\t\t{file_refs[path]} /* {path.name} */ = {{isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = {path.name}; sourceTree = \"<group>\"; }};"
        )
    lines.append(
        f"\t\t{ids['assets_file']} /* Assets.xcassets */ = {{isa = PBXFileReference; lastKnownFileType = folder.assetcatalog; path = Assets.xcassets; sourceTree = \"<group>\"; }};"
    )
    lines.append(
        f"\t\t{ids['plist_file']} /* Info.plist */ = {{isa = PBXFileReference; lastKnownFileType = text.plist.xml; path = Info.plist; sourceTree = \"<group>\"; }};"
    )
    lines.append(
        f"\t\t{ids['signing_xcconfig']} /* Signing.xcconfig */ = {{isa = PBXFileReference; lastKnownFileType = text.xcconfig; path = Signing.xcconfig; sourceTree = \"<group>\"; }};"
    )
    lines.append("/* End PBXFileReference section */")
    lines.append("")

    # PBXFrameworksBuildPhase
    lines.append("/* Begin PBXFrameworksBuildPhase section */")
    lines.append(f"\t\t{ids['frameworks_phase_app']} /* Frameworks */ = {{")
    lines.append("\t\t\tisa = PBXFrameworksBuildPhase;")
    lines.append("\t\t\tbuildActionMask = 2147483647;")
    lines.append("\t\t\tfiles = (")
    lines.append("\t\t\t);")
    lines.append("\t\t\trunOnlyForDeploymentPostprocessing = 0;")
    lines.append("\t\t};")
    lines.append(f"\t\t{ids['frameworks_phase_test']} /* Frameworks */ = {{")
    lines.append("\t\t\tisa = PBXFrameworksBuildPhase;")
    lines.append("\t\t\tbuildActionMask = 2147483647;")
    lines.append("\t\t\tfiles = (")
    lines.append("\t\t\t);")
    lines.append("\t\t\trunOnlyForDeploymentPostprocessing = 0;")
    lines.append("\t\t};")
    lines.append("/* End PBXFrameworksBuildPhase section */")
    lines.append("")

    # PBXGroup
    lines.append("/* Begin PBXGroup section */")
    lines.append(f"\t\t{ids['root_group']} = {{")
    lines.append("\t\t\tisa = PBXGroup;")
    lines.append("\t\t\tchildren = (")
    lines.append(f"\t\t\t\t{ids['app_group']} /* {TARGET_NAME} */,")
    lines.append(f"\t\t\t\t{ids['tests_group']} /* {TEST_TARGET_NAME} */,")
    lines.append(f"\t\t\t\t{ids['products_group']} /* Products */,")
    lines.append(f"\t\t\t\t{ids['signing_xcconfig']} /* Signing.xcconfig */,")
    lines.append("\t\t\t);")
    lines.append("\t\t\tsourceTree = \"<group>\";")
    lines.append("\t\t};")

    lines.append(f"\t\t{ids['products_group']} /* Products */ = {{")
    lines.append("\t\t\tisa = PBXGroup;")
    lines.append("\t\t\tchildren = (")
    lines.append(f"\t\t\t\t{ids['app_product']} /* {TARGET_NAME}.app */,")
    lines.append(f"\t\t\t\t{ids['test_product']} /* {TEST_TARGET_NAME}.xctest */,")
    lines.append("\t\t\t);")
    lines.append("\t\t\tname = Products;")
    lines.append("\t\t\tsourceTree = \"<group>\";")
    lines.append("\t\t};")

    lines.append(f"\t\t{ids['app_group']} /* {TARGET_NAME} */ = {{")
    lines.append("\t\t\tisa = PBXGroup;")
    lines.append("\t\t\tchildren = (")
    for name in subgroup_names:
        lines.append(f"\t\t\t\t{subgroup_ids[name]} /* {name} */,")
    lines.append("\t\t\t);")
    lines.append(f"\t\t\tpath = {TARGET_NAME};")
    lines.append("\t\t\tsourceTree = \"<group>\";")
    lines.append("\t\t};")

    for name in subgroup_names:
        lines.append(f"\t\t{subgroup_ids[name]} /* {name} */ = {{")
        lines.append("\t\t\tisa = PBXGroup;")
        lines.append("\t\t\tchildren = (")
        if name == "Resources":
            lines.append(f"\t\t\t\t{ids['assets_file']} /* Assets.xcassets */,")
            lines.append(f"\t\t\t\t{ids['plist_file']} /* Info.plist */,")
        for path in app_sources:
            if path.parent.name == name:
                lines.append(f"\t\t\t\t{file_refs[path]} /* {path.name} */,")
        lines.append("\t\t\t);")
        lines.append(f"\t\t\tpath = {name};")
        lines.append("\t\t\tsourceTree = \"<group>\";")
        lines.append("\t\t};")

    lines.append(f"\t\t{ids['tests_group']} /* {TEST_TARGET_NAME} */ = {{")
    lines.append("\t\t\tisa = PBXGroup;")
    lines.append("\t\t\tchildren = (")
    for path in test_sources:
        lines.append(f"\t\t\t\t{file_refs[path]} /* {path.name} */,")
    lines.append("\t\t\t);")
    lines.append(f"\t\t\tpath = {TEST_TARGET_NAME};")
    lines.append("\t\t\tsourceTree = \"<group>\";")
    lines.append("\t\t};")
    lines.append("/* End PBXGroup section */")
    lines.append("")

    # PBXNativeTarget
    lines.append("/* Begin PBXNativeTarget section */")
    lines.append(f"\t\t{ids['app_target']} /* {TARGET_NAME} */ = {{")
    lines.append("\t\t\tisa = PBXNativeTarget;")
    lines.append(f"\t\t\tbuildConfigurationList = {ids['app_config_list']} /* Build configuration list for PBXNativeTarget \"{TARGET_NAME}\" */;")
    lines.append("\t\t\tbuildPhases = (")
    lines.append(f"\t\t\t\t{ids['sources_phase_app']} /* Sources */,")
    lines.append(f"\t\t\t\t{ids['frameworks_phase_app']} /* Frameworks */,")
    lines.append(f"\t\t\t\t{ids['resources_phase_app']} /* Resources */,")
    lines.append("\t\t\t);")
    lines.append("\t\t\tbuildRules = (")
    lines.append("\t\t\t);")
    lines.append("\t\t\tdependencies = (")
    lines.append("\t\t\t);")
    lines.append(f"\t\t\tname = {TARGET_NAME};")
    lines.append(f"\t\t\tproductName = {TARGET_NAME};")
    lines.append(f"\t\t\tproductReference = {ids['app_product']} /* {TARGET_NAME}.app */;")
    lines.append("\t\t\tproductType = \"com.apple.product-type.application\";")
    lines.append("\t\t};")

    lines.append(f"\t\t{ids['test_target']} /* {TEST_TARGET_NAME} */ = {{")
    lines.append("\t\t\tisa = PBXNativeTarget;")
    lines.append(f"\t\t\tbuildConfigurationList = {ids['test_config_list']} /* Build configuration list for PBXNativeTarget \"{TEST_TARGET_NAME}\" */;")
    lines.append("\t\t\tbuildPhases = (")
    lines.append(f"\t\t\t\t{ids['sources_phase_test']} /* Sources */,")
    lines.append(f"\t\t\t\t{ids['frameworks_phase_test']} /* Frameworks */,")
    lines.append("\t\t\t);")
    lines.append("\t\t\tbuildRules = (")
    lines.append("\t\t\t);")
    lines.append("\t\t\tdependencies = (")
    lines.append(f"\t\t\t\t{ids['target_dep']} /* PBXTargetDependency */,")
    lines.append("\t\t\t);")
    lines.append(f"\t\t\tname = {TEST_TARGET_NAME};")
    lines.append(f"\t\t\tproductName = {TEST_TARGET_NAME};")
    lines.append(f"\t\t\tproductReference = {ids['test_product']} /* {TEST_TARGET_NAME}.xctest */;")
    lines.append("\t\t\tproductType = \"com.apple.product-type.bundle.unit-test\";")
    lines.append("\t\t};")
    lines.append("/* End PBXNativeTarget section */")
    lines.append("")

    # PBXProject
    lines.append("/* Begin PBXProject section */")
    lines.append(f"\t\t{ids['project']} /* Project object */ = {{")
    lines.append("\t\t\tisa = PBXProject;")
    lines.append(f"\t\t\tattributes = {{")
    lines.append("\t\t\t\tBuildIndependentTargetsInParallel = 1;")
    lines.append("\t\t\t\tLastSwiftUpdateCheck = 1600;")
    lines.append("\t\t\t\tLastUpgradeCheck = 1600;")
    lines.append("\t\t\t\tTargetAttributes = {")
    lines.append(f"\t\t\t\t\t{ids['app_target']} = {{")
    lines.append("\t\t\t\t\t\tCreatedOnToolsVersion = 16.0;")
    lines.append("\t\t\t\t\t};")
    lines.append(f"\t\t\t\t\t{ids['test_target']} = {{")
    lines.append("\t\t\t\t\t\tCreatedOnToolsVersion = 16.0;")
    lines.append(f"\t\t\t\t\t\tTestTargetID = {ids['app_target']};")
    lines.append("\t\t\t\t\t};")
    lines.append("\t\t\t\t};")
    lines.append("\t\t\t};")
    lines.append(f"\t\t\tbuildConfigurationList = {ids['project_config_list']} /* Build configuration list for PBXProject \"{PROJECT_NAME}\" */;")
    lines.append("\t\t\tcompatibilityVersion = \"Xcode 14.0\";")
    lines.append("\t\t\tdevelopmentRegion = en;")
    lines.append("\t\t\thasScannedForEncodings = 0;")
    lines.append("\t\t\tknownRegions = (")
    lines.append("\t\t\t\ten,")
    lines.append("\t\t\t\tBase,")
    lines.append("\t\t\t);")
    lines.append(f"\t\t\tmainGroup = {ids['root_group']};")
    lines.append(f"\t\t\tproductRefGroup = {ids['products_group']} /* Products */;")
    lines.append("\t\t\tprojectDirPath = \"\";")
    lines.append("\t\t\tprojectRoot = \"\";")
    lines.append("\t\t\ttargets = (")
    lines.append(f"\t\t\t\t{ids['app_target']} /* {TARGET_NAME} */,")
    lines.append(f"\t\t\t\t{ids['test_target']} /* {TEST_TARGET_NAME} */,")
    lines.append("\t\t\t);")
    lines.append("\t\t};")
    lines.append("/* End PBXProject section */")
    lines.append("")

    # Resources
    lines.append("/* Begin PBXResourcesBuildPhase section */")
    lines.append(f"\t\t{ids['resources_phase_app']} /* Resources */ = {{")
    lines.append("\t\t\tisa = PBXResourcesBuildPhase;")
    lines.append("\t\t\tbuildActionMask = 2147483647;")
    lines.append("\t\t\tfiles = (")
    lines.append(f"\t\t\t\t{ids['assets_build']} /* Assets.xcassets in Resources */,")
    lines.append("\t\t\t);")
    lines.append("\t\t\trunOnlyForDeploymentPostprocessing = 0;")
    lines.append("\t\t};")
    lines.append("/* End PBXResourcesBuildPhase section */")
    lines.append("")

    # Sources
    lines.append("/* Begin PBXSourcesBuildPhase section */")
    lines.append(f"\t\t{ids['sources_phase_app']} /* Sources */ = {{")
    lines.append("\t\t\tisa = PBXSourcesBuildPhase;")
    lines.append("\t\t\tbuildActionMask = 2147483647;")
    lines.append("\t\t\tfiles = (")
    for path in app_sources:
        lines.append(f"\t\t\t\t{build_files[path]} /* {path.name} in Sources */,")
    lines.append("\t\t\t);")
    lines.append("\t\t\trunOnlyForDeploymentPostprocessing = 0;")
    lines.append("\t\t};")
    lines.append(f"\t\t{ids['sources_phase_test']} /* Sources */ = {{")
    lines.append("\t\t\tisa = PBXSourcesBuildPhase;")
    lines.append("\t\t\tbuildActionMask = 2147483647;")
    lines.append("\t\t\tfiles = (")
    for path in test_sources:
        lines.append(f"\t\t\t\t{build_files[path]} /* {path.name} in Sources */,")
    lines.append("\t\t\t);")
    lines.append("\t\t\trunOnlyForDeploymentPostprocessing = 0;")
    lines.append("\t\t};")
    lines.append("/* End PBXSourcesBuildPhase section */")
    lines.append("")

    # Target dependency
    lines.append("/* Begin PBXTargetDependency section */")
    lines.append(f"\t\t{ids['target_dep']} /* PBXTargetDependency */ = {{")
    lines.append("\t\t\tisa = PBXTargetDependency;")
    lines.append(f"\t\t\ttarget = {ids['app_target']} /* {TARGET_NAME} */;")
    lines.append(f"\t\t\ttargetProxy = {ids['container_proxy']} /* PBXContainerItemProxy */;")
    lines.append("\t\t};")
    lines.append("/* End PBXTargetDependency section */")
    lines.append("")

    # Build configurations
    lines.append("/* Begin XCBuildConfiguration section */")

    def project_config(cid: str, name: str) -> None:
        lines.append(f"\t\t{cid} /* {name} */ = {{")
        lines.append("\t\t\tisa = XCBuildConfiguration;")
        lines.append(f"\t\t\tbaseConfigurationReference = {ids['signing_xcconfig']} /* Signing.xcconfig */;")
        lines.append("\t\t\tbuildSettings = {")
        lines.append("\t\t\t\tALWAYS_SEARCH_USER_PATHS = NO;")
        lines.append("\t\t\t\tCLANG_ENABLE_MODULES = YES;")
        lines.append("\t\t\t\tCLANG_ENABLE_OBJC_ARC = YES;")
        lines.append("\t\t\t\tCOPY_PHASE_STRIP = NO;")
        if name == "Debug":
            lines.append("\t\t\t\tDEBUG_INFORMATION_FORMAT = dwarf;")
            lines.append("\t\t\t\tGCC_DYNAMIC_NO_PIC = NO;")
            lines.append("\t\t\t\tGCC_OPTIMIZATION_LEVEL = 0;")
            lines.append("\t\t\t\tMTL_ENABLE_DEBUG_INFO = INCLUDE_SOURCE;")
            lines.append("\t\t\t\tENABLE_TESTABILITY = YES;")
            lines.append("\t\t\t\tONLY_ACTIVE_ARCH = YES;")
            lines.append("\t\t\t\tSWIFT_ACTIVE_COMPILATION_CONDITIONS = DEBUG;")
            lines.append("\t\t\t\tSWIFT_OPTIMIZATION_LEVEL = \"-Onone\";")
        else:
            lines.append("\t\t\t\tDEBUG_INFORMATION_FORMAT = \"dwarf-with-dsym\";")
            lines.append("\t\t\t\tMTL_ENABLE_DEBUG_INFO = NO;")
            lines.append("\t\t\t\tSWIFT_COMPILATION_MODE = wholemodule;")
        lines.append("\t\t\t\tENABLE_STRICT_OBJC_MSGSEND = YES;")
        lines.append(f"\t\t\t\tIPHONEOS_DEPLOYMENT_TARGET = {MIN_IOS};")
        lines.append("\t\t\t\tSDKROOT = iphoneos;")
        lines.append("\t\t\t};")
        lines.append(f"\t\t\tname = {name};")
        lines.append("\t\t};")

    project_config(ids["project_debug"], "Debug")
    project_config(ids["project_release"], "Release")

    def app_config(cid: str, name: str) -> None:
        lines.append(f"\t\t{cid} /* {name} */ = {{")
        lines.append("\t\t\tisa = XCBuildConfiguration;")
        lines.append(f"\t\t\tbaseConfigurationReference = {ids['signing_xcconfig']} /* Signing.xcconfig */;")
        lines.append("\t\t\tbuildSettings = {")
        lines.append("\t\t\t\tASSETCATALOG_COMPILER_APPICON_NAME = AppIcon;")
        lines.append("\t\t\t\tASSETCATALOG_COMPILER_GLOBAL_ACCENT_COLOR_NAME = AccentColor;")
        lines.append("\t\t\t\tCODE_SIGN_STYLE = Automatic;")
        lines.append(f"\t\t\t\tCURRENT_PROJECT_VERSION = {BUILD_NUMBER};")
        lines.append("\t\t\t\tENABLE_PREVIEWS = YES;")
        lines.append("\t\t\t\tGENERATE_INFOPLIST_FILE = NO;")
        lines.append("\t\t\t\tINFOPLIST_FILE = TesseraVoice/Resources/Info.plist;")
        lines.append(f"\t\t\t\tINFOPLIST_KEY_CFBundleDisplayName = \"{PRODUCT_NAME}\";")
        lines.append("\t\t\t\tLD_RUNPATH_SEARCH_PATHS = (")
        lines.append("\t\t\t\t\t\"$(inherited)\",")
        lines.append("\t\t\t\t\t\"@executable_path/Frameworks\",")
        lines.append("\t\t\t\t);")
        lines.append(f"\t\t\t\tMARKETING_VERSION = {VERSION};")
        lines.append(f"\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = {BUNDLE_ID};")
        lines.append(f"\t\t\t\tPRODUCT_NAME = \"$(TARGET_NAME)\";")
        lines.append("\t\t\t\tSUPPORTED_PLATFORMS = \"iphoneos iphonesimulator\";")
        lines.append("\t\t\t\tSUPPORTS_MACCATALYST = NO;")
        lines.append("\t\t\t\tSWIFT_EMIT_LOC_STRINGS = YES;")
        lines.append("\t\t\t\tSWIFT_VERSION = 5.0;")
        lines.append("\t\t\t\tTARGETED_DEVICE_FAMILY = \"1,2\";")
        lines.append("\t\t\t};")
        lines.append(f"\t\t\tname = {name};")
        lines.append("\t\t};")

    app_config(ids["app_debug"], "Debug")
    app_config(ids["app_release"], "Release")

    def test_config(cid: str, name: str) -> None:
        lines.append(f"\t\t{cid} /* {name} */ = {{")
        lines.append("\t\t\tisa = XCBuildConfiguration;")
        lines.append(f"\t\t\tbaseConfigurationReference = {ids['signing_xcconfig']} /* Signing.xcconfig */;")
        lines.append("\t\t\tbuildSettings = {")
        lines.append("\t\t\t\tBUNDLE_LOADER = \"$(TEST_HOST)\";")
        lines.append("\t\t\t\tCODE_SIGN_STYLE = Automatic;")
        lines.append(f"\t\t\t\tCURRENT_PROJECT_VERSION = {BUILD_NUMBER};")
        lines.append("\t\t\t\tGENERATE_INFOPLIST_FILE = YES;")
        lines.append(f"\t\t\t\tMARKETING_VERSION = {VERSION};")
        lines.append(f"\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = {TEST_BUNDLE_ID};")
        lines.append(f"\t\t\t\tPRODUCT_NAME = \"$(TARGET_NAME)\";")
        lines.append("\t\t\t\tSUPPORTED_PLATFORMS = \"iphoneos iphonesimulator\";")
        lines.append("\t\t\t\tSWIFT_VERSION = 5.0;")
        lines.append("\t\t\t\tTARGETED_DEVICE_FAMILY = \"1,2\";")
        lines.append(f"\t\t\t\tTEST_HOST = \"$(BUILT_PRODUCTS_DIR)/{TARGET_NAME}.app/$(BUNDLE_EXECUTABLE_FOLDER_PATH)/{TARGET_NAME}\";")
        lines.append("\t\t\t};")
        lines.append(f"\t\t\tname = {name};")
        lines.append("\t\t};")

    test_config(ids["test_debug"], "Debug")
    test_config(ids["test_release"], "Release")
    lines.append("/* End XCBuildConfiguration section */")
    lines.append("")

    # Config lists
    lines.append("/* Begin XCConfigurationList section */")
    lines.append(f"\t\t{ids['project_config_list']} /* Build configuration list for PBXProject \"{PROJECT_NAME}\" */ = {{")
    lines.append("\t\t\tisa = XCConfigurationList;")
    lines.append("\t\t\tbuildConfigurations = (")
    lines.append(f"\t\t\t\t{ids['project_debug']} /* Debug */,")
    lines.append(f"\t\t\t\t{ids['project_release']} /* Release */,")
    lines.append("\t\t\t);")
    lines.append("\t\t\tdefaultConfigurationIsVisible = 0;")
    lines.append("\t\t\tdefaultConfigurationName = Release;")
    lines.append("\t\t};")
    lines.append(f"\t\t{ids['app_config_list']} /* Build configuration list for PBXNativeTarget \"{TARGET_NAME}\" */ = {{")
    lines.append("\t\t\tisa = XCConfigurationList;")
    lines.append("\t\t\tbuildConfigurations = (")
    lines.append(f"\t\t\t\t{ids['app_debug']} /* Debug */,")
    lines.append(f"\t\t\t\t{ids['app_release']} /* Release */,")
    lines.append("\t\t\t);")
    lines.append("\t\t\tdefaultConfigurationIsVisible = 0;")
    lines.append("\t\t\tdefaultConfigurationName = Release;")
    lines.append("\t\t};")
    lines.append(f"\t\t{ids['test_config_list']} /* Build configuration list for PBXNativeTarget \"{TEST_TARGET_NAME}\" */ = {{")
    lines.append("\t\t\tisa = XCConfigurationList;")
    lines.append("\t\t\tbuildConfigurations = (")
    lines.append(f"\t\t\t\t{ids['test_debug']} /* Debug */,")
    lines.append(f"\t\t\t\t{ids['test_release']} /* Release */,")
    lines.append("\t\t\t);")
    lines.append("\t\t\tdefaultConfigurationIsVisible = 0;")
    lines.append("\t\t\tdefaultConfigurationName = Release;")
    lines.append("\t\t};")
    lines.append("/* End XCConfigurationList section */")
    lines.append("\t};")
    lines.append(f"\trootObject = {ids['project']} /* Project object */;")
    lines.append("}")

    project_dir = ROOT / f"{PROJECT_NAME}.xcodeproj"
    project_dir.mkdir(parents=True, exist_ok=True)
    pbx = project_dir / "project.pbxproj"
    pbx.write_text("\n".join(lines) + "\n", encoding="utf-8")

    scheme_dir = project_dir / "xcshareddata" / "xcschemes"
    scheme_dir.mkdir(parents=True, exist_ok=True)
    scheme = f"""<?xml version="1.0" encoding="UTF-8"?>
<Scheme
   LastUpgradeVersion = "1600"
   version = "1.7">
   <BuildAction
      parallelizeBuildables = "YES"
      buildImplicitDependencies = "YES">
      <BuildActionEntries>
         <BuildActionEntry
            buildForTesting = "YES"
            buildForRunning = "YES"
            buildForProfiling = "YES"
            buildForArchiving = "YES"
            buildForAnalyzing = "YES">
            <BuildableReference
               BuildableIdentifier = "primary"
               BlueprintIdentifier = "{ids['app_target']}"
               BuildableName = "{TARGET_NAME}.app"
               BlueprintName = "{TARGET_NAME}"
               ReferencedContainer = "container:{PROJECT_NAME}.xcodeproj">
            </BuildableReference>
         </BuildActionEntry>
      </BuildActionEntries>
   </BuildAction>
   <TestAction
      buildConfiguration = "Debug"
      selectedDebuggerIdentifier = "Xcode.DebuggerFoundation.Debugger.LLDB"
      selectedLauncherIdentifier = "Xcode.DebuggerFoundation.Launcher.LLDB"
      shouldUseLaunchSchemeArgsEnv = "YES"
      shouldAutocreateTestPlan = "YES">
      <Testables>
         <TestableReference
            skipped = "NO"
            parallelizable = "YES">
            <BuildableReference
               BuildableIdentifier = "primary"
               BlueprintIdentifier = "{ids['test_target']}"
               BuildableName = "{TEST_TARGET_NAME}.xctest"
               BlueprintName = "{TEST_TARGET_NAME}"
               ReferencedContainer = "container:{PROJECT_NAME}.xcodeproj">
            </BuildableReference>
         </TestableReference>
      </Testables>
   </TestAction>
   <LaunchAction
      buildConfiguration = "Debug"
      selectedDebuggerIdentifier = "Xcode.DebuggerFoundation.Debugger.LLDB"
      selectedLauncherIdentifier = "Xcode.DebuggerFoundation.Launcher.LLDB"
      launchStyle = "0"
      useCustomWorkingDirectory = "NO"
      ignoresPersistentStateOnLaunch = "NO"
      debugDocumentVersioning = "YES"
      debugServiceExtension = "internal"
      allowLocationSimulation = "YES">
      <BuildableProductRunnable
         runnableDebuggingMode = "0">
         <BuildableReference
            BuildableIdentifier = "primary"
            BlueprintIdentifier = "{ids['app_target']}"
            BuildableName = "{TARGET_NAME}.app"
            BlueprintName = "{TARGET_NAME}"
            ReferencedContainer = "container:{PROJECT_NAME}.xcodeproj">
         </BuildableReference>
      </BuildableProductRunnable>
   </LaunchAction>
   <ProfileAction
      buildConfiguration = "Release"
      shouldUseLaunchSchemeArgsEnv = "YES"
      savedToolIdentifier = ""
      useCustomWorkingDirectory = "NO"
      debugDocumentVersioning = "YES">
      <BuildableProductRunnable
         runnableDebuggingMode = "0">
         <BuildableReference
            BuildableIdentifier = "primary"
            BlueprintIdentifier = "{ids['app_target']}"
            BuildableName = "{TARGET_NAME}.app"
            BlueprintName = "{TARGET_NAME}"
            ReferencedContainer = "container:{PROJECT_NAME}.xcodeproj">
         </BuildableReference>
      </BuildableProductRunnable>
   </ProfileAction>
   <AnalyzeAction
      buildConfiguration = "Debug">
   </AnalyzeAction>
   <ArchiveAction
      buildConfiguration = "Release"
      revealArchiveInOrganizer = "YES">
   </ArchiveAction>
</Scheme>
"""
    (scheme_dir / f"{TARGET_NAME}.xcscheme").write_text(scheme, encoding="utf-8")
    print(f"Wrote {pbx}")
    print(f"App sources: {len(app_sources)}")
    print(f"Test sources: {len(test_sources)}")
    print(f"App target id: {ids['app_target']}")


if __name__ == "__main__":
    main()
