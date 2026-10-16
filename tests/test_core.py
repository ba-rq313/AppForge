import io
import zipfile
from pathlib import Path

import pytest

from app.core import parse_play_store_url, prepare_android_source, valid_package_id


def test_parse_google_play_url():
    assert parse_play_store_url("https://play.google.com/store/apps/details?id=com.example.app") == "com.example.app"


@pytest.mark.parametrize("url", [
    "http://play.google.com/store/apps/details?id=com.example.app",
    "https://evil.example/store/apps/details?id=com.example.app",
    "https://play.google.com/store/apps/details?id=../other",
    "https://play.google.com/store/apps/category/GAME",
])
def test_reject_bad_play_store_url(url):
    with pytest.raises(ValueError):
        parse_play_store_url(url)


def test_package_id_validation():
    assert valid_package_id("com.myname.newapp")
    assert not valid_package_id("Com.Example.App")
    assert not valid_package_id("one")


def make_source_zip() -> bytes:
    files = {
        "Demo/settings.gradle": "rootProject.name = 'Demo'\ninclude ':app'\n",
        "Demo/build.gradle": "plugins { id 'com.android.application' version '8.6.1' apply false }\n",
        "Demo/app/build.gradle": "plugins { id 'com.android.application' }\nandroid {\n    namespace 'com.demo.app'\n    defaultConfig {\n        applicationId 'com.demo.app'\n    }\n}\n",
        "Demo/app/src/main/AndroidManifest.xml": '<manifest xmlns:android="http://schemas.android.com/apk/res/android"><application android:label="@string/app_name" android:icon="@mipmap/ic_launcher"></application></manifest>',
        "Demo/app/src/main/res/values/strings.xml": '<resources><string name="app_name">Demo</string></resources>',
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return output.getvalue()


def test_prepare_source_changes_name_and_app_id():
    output, work, warnings = prepare_android_source(make_source_zip(), "New Name", "com.user.newapp")
    try:
        with zipfile.ZipFile(output) as zf:
            names = zf.namelist()
            gradle = zf.read("Demo/app/build.gradle").decode()
            strings = zf.read("Demo/app/src/main/res/values/strings.xml").decode()
        assert 'applicationId "com.user.newapp"' in gradle
        assert "New Name" in strings
        assert "app/src/main/AndroidManifest.xml" in "\n".join(names)
    finally:
        work.cleanup()


def test_reject_zip_traversal():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zf:
        zf.writestr("../escape.txt", "bad")
    with pytest.raises(ValueError):
        prepare_android_source(output.getvalue(), "New Name", "com.user.newapp")
