#!/usr/bin/env python3
"""Add the physical UI journey target to a generated Xcode project."""
import json
from pathlib import Path
import plistlib
import subprocess
import uuid
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parents[1]
project_file = root / "TesseraVoice.xcodeproj/project.pbxproj"
parsed = subprocess.run(["plutil", "-convert", "json", "-o", "-", str(project_file)], check=True, capture_output=True, text=True)
project = json.loads(parsed.stdout)
objects = project["objects"]
project_object = objects[project["rootObject"]]
app_id = None
for key, value in objects.items():
    if value.get("isa") == "PBXNativeTarget" and value.get("name") == "TesseraVoiceUITests":
        raise SystemExit("UI target already exists")
    if value.get("isa") == "PBXNativeTarget" and value.get("name") == "TesseraVoice":
        app_id = key
if app_id is None:
    raise SystemExit("App target missing")
app = objects[app_id]
app_configs = objects[app["buildConfigurationList"]]["buildConfigurations"]


def add(value):
    identifier = uuid.uuid4().hex[:24].upper()
    objects[identifier] = value
    return identifier


source = add({"isa": "PBXFileReference", "lastKnownFileType": "sourcecode.swift", "path": "TesseraVoiceUITests/CallJourneyTests.swift", "sourceTree": "<group>"})
product = add({"isa": "PBXFileReference", "explicitFileType": "wrapper.cfbundle", "path": "TesseraVoiceUITests.xctest", "sourceTree": "BUILT_PRODUCTS_DIR"})
build_file = add({"isa": "PBXBuildFile", "fileRef": source})
sources = add({"isa": "PBXSourcesBuildPhase", "buildActionMask": "2147483647", "files": [build_file], "runOnlyForDeploymentPostprocessing": "0"})
configs = []
for config_id in app_configs:
    app_config = objects[config_id]
    settings = app_config["buildSettings"]
    test_settings = {
        "CODE_SIGN_STYLE": "Automatic",
        "GENERATE_INFOPLIST_FILE": "YES",
        "IPHONEOS_DEPLOYMENT_TARGET": settings.get("IPHONEOS_DEPLOYMENT_TARGET", "17.0"),
        "PRODUCT_BUNDLE_IDENTIFIER": settings["PRODUCT_BUNDLE_IDENTIFIER"] + ".uitests",
        "PRODUCT_NAME": "$(TARGET_NAME)",
        "SDKROOT": "iphoneos",
        "SWIFT_VERSION": "5.0",
        "TARGETED_DEVICE_FAMILY": "1",
        "TEST_TARGET_NAME": "TesseraVoice",
    }
    config = {"isa": "XCBuildConfiguration", "name": app_config["name"], "buildSettings": test_settings}
    base = app_config.get("baseConfigurationReference")
    if base:
        config["baseConfigurationReference"] = base
    configs.append(add(config))
config_list = add({"isa": "XCConfigurationList", "buildConfigurations": configs, "defaultConfigurationIsVisible": "0", "defaultConfigurationName": "Debug"})
proxy = add({"isa": "PBXContainerItemProxy", "containerPortal": project["rootObject"], "proxyType": "1", "remoteGlobalIDString": app_id, "remoteInfo": "TesseraVoice"})
dependency = add({"isa": "PBXTargetDependency", "target": app_id, "targetProxy": proxy})
target = add({"isa": "PBXNativeTarget", "name": "TesseraVoiceUITests", "productName": "TesseraVoiceUITests", "productType": "com.apple.product-type.bundle.ui-testing", "productReference": product, "buildConfigurationList": config_list, "buildPhases": [sources], "dependencies": [dependency], "buildRules": []})
project_object["targets"].append(target)
objects[project_object["mainGroup"]]["children"].extend([source, product])
project_file.write_bytes(plistlib.dumps(project, sort_keys=False))
scheme_path = root / "TesseraVoice.xcodeproj/xcshareddata/xcschemes/TesseraVoice.xcscheme"
scheme = ET.parse(scheme_path)
testables = scheme.getroot().find("TestAction/Testables")
testable = ET.SubElement(testables, "TestableReference", {"skipped": "NO"})
ET.SubElement(testable, "BuildableReference", {"BuildableIdentifier": "primary", "BlueprintIdentifier": target, "BuildableName": "TesseraVoiceUITests.xctest", "BlueprintName": "TesseraVoiceUITests", "ReferencedContainer": "container:TesseraVoice.xcodeproj"})
scheme.write(scheme_path, encoding="UTF-8", xml_declaration=True)
print("Physical UI journey target added")
