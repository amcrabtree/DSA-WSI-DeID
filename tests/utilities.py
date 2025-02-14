import os
import shutil
import sys

import pytest
from girder.models.setting import Setting

from wsi_deid.constants import PluginSettings

from .datastore import datastore


@pytest.fixture
def provisionServer(server, admin, fsAssetstore, tmp_path):
    yield _provisionServer(tmp_path)


@pytest.fixture
def provisionBoundServer(boundServer, admin, fsAssetstore, tmp_path):
    yield _provisionServer(tmp_path)


def _provisionServer(tmp_path):
    sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'devops', 'wsi_deid'))
    import provision  # noqa
    provision.provision()
    del sys.path[-1]

    importPath = tmp_path / 'import'
    os.makedirs(importPath, exist_ok=True)
    exportPath = tmp_path / 'export'
    os.makedirs(exportPath, exist_ok=True)
    Setting().set(PluginSettings.WSI_DEID_IMPORT_PATH, str(importPath))
    Setting().set(PluginSettings.WSI_DEID_EXPORT_PATH, str(exportPath))
    for filename in {'aperio_jp2k.svs', 'hamamatsu.ndpi', 'philips.ptif'}:
        path = datastore.fetch(filename)
        shutil.copy(path, str(importPath / filename))
    dataPath = os.path.join(os.path.dirname(__file__), 'data')
    for filename in {'deidUpload.csv'}:
        path = os.path.join(dataPath, filename)
        shutil.copy(path, importPath / filename)
    return importPath, exportPath
