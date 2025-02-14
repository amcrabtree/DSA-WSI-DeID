import copy
import datetime
import os
import re
import tempfile
import threading
import time

import girder_large_image
import histomicsui.handlers
from bson import ObjectId
from girder import logger
from girder.api import access
from girder.api.describe import Description, autoDescribeRoute
from girder.api.rest import Resource
from girder.constants import AccessType, SortDir, TokenScope
from girder.exceptions import AccessException, RestException
from girder.models.file import File
from girder.models.folder import Folder
from girder.models.item import Item
from girder.models.setting import Setting
from girder.models.upload import Upload
from girder.models.user import User
from girder.utility.model_importer import ModelImporter
from girder.utility.progress import ProgressContext, setResponseTimeLimit
from girder_jobs.models.job import Job
from girder_large_image.models.image_item import ImageItem

from . import config, import_export, process
from .constants import PluginSettings, TokenOnlyPrefix

ProjectFolders = {
    'ingest': PluginSettings.HUI_INGEST_FOLDER,
    'quarantine': PluginSettings.HUI_QUARANTINE_FOLDER,
    'processed': PluginSettings.HUI_PROCESSED_FOLDER,
    'rejected': PluginSettings.HUI_REJECTED_FOLDER,
    'original': PluginSettings.HUI_ORIGINAL_FOLDER,
    'finished': PluginSettings.HUI_FINISHED_FOLDER,
    'unfiled': PluginSettings.WSI_DEID_UNFILED_FOLDER,
}


IngestLock = threading.Lock()
ExportLock = threading.Lock()
ItemActionLock = threading.Lock()
ItemActionList = []


def create_folder_hierarchy(item, user, folder):
    """
    Create a folder hierarchy that matches the original if the original is
    under a project folder.

    :param item: the item that will be moved or copied.
    :param user: the user that will own the created folders.
    :param folder: the destination project folder.
    :returns: a destination folder that is either the folder passed to this
        routine or a folder beneath it.
    """
    # Mirror the folder structure in the destination.  Remove empty folders in
    # the original location.
    projFolderIds = [Setting().get(ProjectFolders[key]) for key in ProjectFolders]
    origPath = []
    origFolders = []
    itemFolder = Folder().load(item['folderId'], force=True)
    while itemFolder and str(itemFolder['_id']) not in projFolderIds:
        origPath.insert(0, itemFolder['name'])
        origFolders.insert(0, itemFolder)
        if itemFolder['parentCollection'] != 'folder':
            origPath = origFolders = []
            itemFolder = None
        else:
            itemFolder = Folder().load(itemFolder['parentId'], force=True)
    # create new folder structure
    for name in origPath:
        folder = Folder().createFolder(folder, name=name, creator=user, reuseExisting=True)
    return folder, origFolders


def move_item(item, user, settingkey):
    """
    Move an item to one of the folders specified by a setting.

    :param item: the item model to move.
    :param user: a user for folder creation.
    :param settingkey: one of the PluginSettings values.
    :returns: the item after move.
    """
    folderId = Setting().get(settingkey)
    if not folderId:
        raise RestException('The appropriate folder is not configured.')
    folder = Folder().load(folderId, force=True)
    if not folder:
        raise RestException('The appropriate folder does not exist.')
    if str(folder['_id']) == str(item['folderId']):
        raise RestException('The item is already in the appropriate folder.')
    folder, origFolders = create_folder_hierarchy(item, user, folder)
    if settingkey == PluginSettings.HUI_QUARANTINE_FOLDER:
        quarantineInfo = {
            'originalFolderId': item['folderId'],
            'originalBaseParentType': item['baseParentType'],
            'originalBaseParentId': item['baseParentId'],
            'originalUpdated': item['updated'],
            'quarantineUserId': user['_id'],
            'quarantineTime': datetime.datetime.utcnow()
        }
    # move the item
    item = Item().move(item, folder)
    if settingkey == PluginSettings.HUI_QUARANTINE_FOLDER:
        # When quarantining, add metadata and don't prune folders
        item = Item().setMetadata(item, {'quarantine': quarantineInfo})
    else:
        # Prune empty folders
        for origFolder in origFolders[::-1]:
            if Folder().findOne({'parentId': origFolder['_id'], 'parentCollection': 'folder'}):
                break
            if Item().findOne({'folderId': origFolder['_id']}):
                break
            Folder().remove(origFolder)
    return item


def quarantine_item(item, user, *args, **kwargs):
    return move_item(item, user, PluginSettings.HUI_QUARANTINE_FOLDER)


histomicsui.handlers.quarantine_item = quarantine_item


def process_item(item, user=None):
    """
    Copy an item to the original folder.  Modify the item by processing it and
    generating a new, redacted file.  Move the item to the processed folder.

    :param item: the item model to move.
    :param user: the user performing the processing.
    :returns: the item after move.
    """
    from . import __version__

    origFolderId = Setting().get(PluginSettings.HUI_ORIGINAL_FOLDER)
    procFolderId = Setting().get(PluginSettings.HUI_PROCESSED_FOLDER)
    if not origFolderId or not procFolderId:
        raise RestException('The appropriate folder is not configured.')
    origFolder = Folder().load(origFolderId, force=True)
    procFolder = Folder().load(procFolderId, force=True)
    if not origFolder or not procFolder:
        raise RestException('The appropriate folder does not exist.')
    creator = User().load(item['creatorId'], force=True)
    # Generate the redacted file first, so if it fails we don't do anything
    # else
    with tempfile.TemporaryDirectory(prefix='wsi_deid') as tempdir:
        try:
            filepath, info = process.redact_item(item, tempdir)
        except Exception as e:
            logger.exception('Failed to redact item')
            raise RestException(e.args[0])
        origFolder, _ = create_folder_hierarchy(item, user, origFolder)
        origItem = Item().copyItem(item, creator, folder=origFolder)
        origItem = Item().setMetadata(origItem, {
            'wsi_deidProcessed': {
                'itemId': str(item['_id']),
                'time': datetime.datetime.utcnow().isoformat(),
                'user': str(user['_id']) if user else None,
            },
        })
        ImageItem().delete(item)
        origSize = 0
        for childFile in Item().childFiles(item):
            origSize += childFile['size']
            File().remove(childFile)
        newName = item['name']
        if len(os.path.splitext(newName)[1]) <= 1:
            newName = os.path.splitext(item['name'])[0] + os.path.splitext(filepath)[1]
        newSize = os.path.getsize(filepath)
        with open(filepath, 'rb') as f:
            Upload().uploadFromFile(
                f, size=os.path.getsize(filepath), name=newName,
                parentType='item', parent=item, user=creator,
                mimeType=info['mimetype'])
        item = Item().load(item['_id'], force=True)
        item['name'] = newName
    item.setdefault('meta', {})
    item['meta'].setdefault('redacted', [])
    item['meta']['redacted'].append({
        'user': str(user['_id']) if user else None,
        'time': datetime.datetime.utcnow().isoformat(),
        'originalSize': origSize,
        'redactedSize': newSize,
        'redactList': item['meta'].get('redactList'),
        'details': info,
        'version': __version__,
    })
    item['meta'].pop('quarantine', None)
    allPreviousExports = {}
    for history_key in [import_export.EXPORT_HISTORY_KEY, import_export.SFTP_HISTORY_KEY]:
        if history_key in item['meta']:
            allPreviousExports[history_key] = item['meta'][history_key]
        item['meta'].pop(history_key, None)
    item['meta']['redacted'][-1]['previousExports'] = allPreviousExports
    item['updated'] = datetime.datetime.utcnow()
    try:
        redactList = item['meta'].get('redactList') or {}
        if redactList.get('area', {}).get('_wsi', {}).get('geojson') or any(
                redactList['images'][key].get('geojson') for key in redactList.get('images', {})):
            ImageItem().removeThumbnailFiles(item)

    except Exception:
        ImageItem().removeThumbnailFiles(item)
    item = move_item(item, user, PluginSettings.HUI_PROCESSED_FOLDER)
    return item


def ocr_item(item, user):
    job_title = f'Finding label text for image: {item["name"]}'
    ocr_job = Job().createLocalJob(
        module='wsi_deid.jobs',
        function='start_ocr_item_job',
        title=job_title,
        type='wsi_deid.ocr_job',
        user=user,
        asynchronous=True,
        args=(item,)
    )
    Job().scheduleJob(job=ocr_job)
    return {
        'jobId': ocr_job.get('_id', None),
    }


def get_first_item(folder, user, exclude=None, excludeFolders=False):
    """
    Get the first item in a folder or any subfolder of that folder.  The items
    are sorted alphabetically.

    :param folder: the folder to search
    :param user: the user with permissions to use for searching.
    :param exclude: if not None, exclude items in this list of folders (does
        not include their subfolders).
    :param excludeFolders: if True, add the folders of items in the current
        ItemActionList to the list of excluded folders.
    :returns: an item or None.
    """
    if excludeFolders:
        exclude = (exclude or [])[:]
        with ItemActionLock:
            for item in ItemActionList:
                exclude.append({'_id': item['folderId']})
    excludeset = (str(entry['_id']) for entry in exclude) if exclude else set()
    if str(folder['_id']) not in excludeset:
        for item in Folder().childItems(folder, limit=1, sort=[('lowerName', SortDir.ASCENDING)]):
            with ItemActionLock:
                if item['_id'] not in [entry['_id'] for entry in ItemActionList]:
                    return item
    for subfolder in Folder().childFolders(
            folder, 'folder', user=user, sort=[('lowerName', SortDir.ASCENDING)]):
        item = get_first_item(subfolder, user, exclude)
        if item is not None and str(subfolder['_id']) not in excludeset:
            with ItemActionLock:
                if item['_id'] not in [entry['_id'] for entry in ItemActionList]:
                    return item


def ingestData(user=None, progress=True):
    """
    Ingest data from the import folder.

    :param user: the user that started this.
    """
    with ProgressContext(progress, user=user, title='Importing data') as ctx:
        with IngestLock:
            result = import_export.ingestData(ctx, user)
    result['action'] = 'ingest'
    return result


def exportData(user=None, progress=True):
    """
    Export data to the export folder.

    :param user: the user that started this.
    """
    with ProgressContext(progress, user=user, title='Exporting recent finished items') as ctx:
        with ExportLock:
            result = import_export.exportItems(ctx, user)
    result['action'] = 'export'
    return result


class WSIDeIDResource(Resource):
    def __init__(self):
        super().__init__()
        self.resourceName = 'wsi_deid'
        self.route('GET', ('project_folder', ':id'), self.isProjectFolder)
        self.route('GET', ('next_unprocessed_item', ), self.nextUnprocessedItem)
        self.route('GET', ('next_unprocessed_folders', ), self.nextUnprocessedFolders)
        self.route('PUT', ('item', ':id', 'action', 'refile'), self.refileItem)
        self.route('PUT', ('item', ':id', 'action', ':action'), self.itemAction)
        self.route('PUT', ('item', ':id', 'redactList'), self.setRedactList)
        self.route('GET', ('item', ':id', 'refileList'), self.getRefileList)
        self.route('PUT', ('action', 'ingest'), self.ingest)
        self.route('PUT', ('action', 'export'), self.export)
        self.route('PUT', ('action', 'exportall'), self.exportAll)
        # self.route('PUT', ('action', 'finishlist'), self.finishItemList)
        self.route('PUT', ('action', 'ocrall'), self.ocrReadyToProcess)
        self.route('PUT', ('action', 'list', ':action'), self.itemListAction)
        self.route('GET', ('settings',), self.getSettings)
        self.route('GET', ('resource', ':id', 'subtreeCount'), self.getSubtreeCount)
        self.route('GET', ('folder', ':id', 'item_list'), self.folderItemList)

    @autoDescribeRoute(
        Description('Check if a folder is a project folder.')
        .modelParam('id', model=Folder, level=AccessType.READ)
        .errorResponse()
        .errorResponse('Write access was denied on the folder.', 403)
    )
    @access.public(scope=TokenScope.DATA_READ)
    def isProjectFolder(self, folder):
        while folder:
            for key in ProjectFolders:
                projFolderId = Setting().get(ProjectFolders[key])
                if str(folder['_id']) == projFolderId:
                    return key
            if folder['parentCollection'] != 'folder':
                break
            folder = Folder().load(folder['parentId'], force=True)
        return None

    def _actionForItem(self, item, user, action):
        """
        Given an item, user, an action, return a function and parameters to
        execute that action.

        :param item: an item document.
        :param user: the user document.
        :param action: an action string.
        :returns: the action function, a tuple of arguments to pass to it, the
            name of the action, and the present participle of the action.
        """
        actionmap = {
            'quarantine': (
                histomicsui.handlers.quarantine_item, (item, user, False),
                'quarantine', 'quarantining'),
            'unquarantine': (
                histomicsui.handlers.restore_quarantine_item, (item, user),
                'unquarantine', 'unquaranting'),
            'reject': (
                move_item, (item, user, PluginSettings.HUI_REJECTED_FOLDER),
                'reject', 'rejecting'),
            'finish': (
                move_item, (item, user, PluginSettings.HUI_FINISHED_FOLDER),
                'approve', 'approving'),
            'process': (
                process_item, (item, user),
                'redact', 'redacting'),
            'ocr': (
                ocr_item, (item, user),
                'scan', 'scanning'),
        }
        return actionmap[action]

    @autoDescribeRoute(
        Description('Perform an action on an item.')
        # Allow all users to do redaction actions; change to WRITE otherwise
        .modelParam('id', model=Item, level=AccessType.READ)
        .param('action', 'Action to perform on the item.  One of process, '
               'reject, quarantine, unquarantine, finish, ocr.', paramType='path',
               enum=['process', 'reject', 'quarantine', 'unquarantine', 'finish', 'ocr'])
        .errorResponse()
        .errorResponse('Write access was denied on the item.', 403)
    )
    @access.user
    def itemAction(self, item, action):
        setResponseTimeLimit(86400)
        user = self.getCurrentUser()
        with ItemActionLock:
            ItemActionList.append(item)
        try:
            actionfunc, actionargs, name, pp = self._actionForItem(item, user, action)
        finally:
            with ItemActionLock:
                ItemActionList.remove(item)
        return actionfunc(*actionargs)

    @autoDescribeRoute(
        Description('Set the redactList meta value on an item.')
        .responseClass('Item')
        # we allow all users to do this; change to WRITE to do otherwise.
        .modelParam('id', model=Item, level=AccessType.READ)
        .jsonParam('redactList', 'A JSON object containing the redactList to set',
                   paramType='body', requireObject=True)
        .errorResponse()
    )
    @access.user
    def setRedactList(self, item, redactList):
        return Item().setMetadata(item, {'redactList': redactList})

    @autoDescribeRoute(
        Description('Ingest data from the import folder asynchronously.')
        .errorResponse()
    )
    @access.user
    def ingest(self):
        setResponseTimeLimit(86400)
        user = self.getCurrentUser()
        return ingestData(user)

    @autoDescribeRoute(
        Description('Export recently finished items to the export folder asynchronously.')
        .errorResponse()
    )
    @access.user
    def export(self):
        setResponseTimeLimit(86400)
        user = self.getCurrentUser()
        return exportData(user)

    @autoDescribeRoute(
        Description('Export all finished items to the export folder asynchronously.')
        .errorResponse()
    )
    @access.user
    def exportAll(self):
        setResponseTimeLimit(86400)
        user = self.getCurrentUser()
        with ProgressContext(True, user=user, title='Exporting all finished items') as ctx:
            result = import_export.exportItems(ctx, user, True)
        result['action'] = 'exportall'
        return result

    @autoDescribeRoute(
        Description('Run OCR to find label text on items in the import folder without OCR metadata')
        .errorResponse()
    )
    @access.user
    def ocrReadyToProcess(self):
        user = self.getCurrentUser()
        itemIds = []
        ingestFolder = Folder().load(Setting().get(
            PluginSettings.HUI_INGEST_FOLDER), user=user, level=AccessType.WRITE
        )
        resp = {'action': 'ocrall'}
        for _, file in Folder().fileList(ingestFolder, user, data=False):
            itemId = file['itemId']
            item = Item().load(itemId, force=True)
            if (item.get('meta', {}).get('label_ocr', None) is None and
                    item.get('meta', {}).get('macro_ocr', None) is None):
                itemIds.append(file['itemId'])
        if len(itemIds) > 0:
            jobStart = datetime.datetime.now().strftime('%Y%m%d %H%M%S')
            batchJob = Job().createLocalJob(
                module='wsi_deid.jobs',
                function='start_ocr_batch_job',
                title=f'Batch OCR triggered manually: {user["login"]}, {jobStart}',
                type='wsi_deid.batch_ocr',
                user=user,
                asynchronous=True,
                args=(itemIds,),
            )
            Job().scheduleJob(job=batchJob)
            resp['ocrJobId'] = batchJob['_id']
        return resp

    @autoDescribeRoute(
        Description('Get the ID of the next unprocessed item.')
        .errorResponse()
    )
    @access.user
    def nextUnprocessedItem(self):
        user = self.getCurrentUser()
        for settingKey in (
                PluginSettings.WSI_DEID_UNFILED_FOLDER,
                PluginSettings.HUI_INGEST_FOLDER,
                PluginSettings.HUI_QUARANTINE_FOLDER,
                PluginSettings.HUI_PROCESSED_FOLDER):
            folderId = Setting().get(settingKey)
            if folderId:
                try:
                    folder = Folder().load(folderId, user=user, level=AccessType.READ)
                except AccessException:
                    # Don't return a result if we don't have read access
                    continue
                if folder:
                    item = get_first_item(folder, user)
                    if item is not None:
                        return str(item['_id'])

    @autoDescribeRoute(
        Description(
            'Get the IDs of the next two folders with unprocessed items and '
            'the id of the finished folder.')
        .errorResponse()
    )
    @access.user
    def nextUnprocessedFolders(self):
        user = self.getCurrentUser()
        folders = []
        exclude = None
        for _ in range(2):
            for settingKey in (
                    PluginSettings.HUI_INGEST_FOLDER,
                    PluginSettings.HUI_QUARANTINE_FOLDER,
                    PluginSettings.HUI_PROCESSED_FOLDER):
                folder = Folder().load(Setting().get(settingKey), user=user, level=AccessType.READ)
                item = get_first_item(folder, user, exclude, exclude is not None)
                if item is not None:
                    parent = Folder().load(item['folderId'], user=user, level=AccessType.READ)
                    folders.append(str(parent['_id']))
                    exclude = [parent]
                    break
            if not exclude:
                break
        folders.append(Setting().get(PluginSettings.HUI_FINISHED_FOLDER))
        return folders

    @autoDescribeRoute(
        Description('Get settings that affect the UI.')
        .errorResponse()
    )
    @access.public(scope=TokenScope.DATA_READ)
    def getSettings(self):
        return config.getConfig()

    @access.public
    @autoDescribeRoute(
        Description('Get total subtree folder and item counts of a resource by ID.')
        .param('id', 'The ID of the resource.', paramType='path')
        .param('type', 'The type of the resource (folder, user, collection).')
        .errorResponse('ID was invalid.')
        .errorResponse('Read access was denied for the resource.', 403)
    )
    def getSubtreeCount(self, id, type):
        user = self.getCurrentUser()
        model = ModelImporter.model(type)
        doc = model.load(id=id, user=self.getCurrentUser(), level=AccessType.READ)
        folderCount = model.subtreeCount(doc, False, user=user, level=AccessType.READ)
        totalCount = model.subtreeCount(doc, True, user=user, level=AccessType.READ)
        return {'folders': folderCount, 'items': totalCount - folderCount, 'total': totalCount}

    def _folderItemListGetItem(self, item):
        try:
            metadata = ImageItem().getMetadata(item)
        except Exception:
            return None
        internal_metadata = ImageItem().getInternalMetadata(item)
        images = ImageItem().getAssociatedImagesList(item)
        return {
            'item': item,
            'metadata': metadata,
            'internal_metadata': internal_metadata,
            'images': images,
        }

    def _commonValues(self, common, entry):
        if common is None:
            return copy.deepcopy(entry)
        for k, v in entry.items():
            if isinstance(v, dict):
                if isinstance(common.get(k), dict):
                    self._commonValues(common[k], v)
                elif k in common:
                    del common[k]
            elif k in common and common.get(k) != v:
                del common[k]
        for k in list(common.keys()):
            if k not in entry:
                del common[k]
        return common

    def _allKeys(self, allkeys, entry, parent=None):
        for k, v in entry.items():
            subkey = tuple(list(parent or ()) + [k])
            if isinstance(v, dict):
                self._allKeys(allkeys, v, subkey)
            else:
                allkeys.add(subkey)

    @autoDescribeRoute(
        Description(
            'Return a list of all items in a folder with enough information '
            'to allow review and redaction.')
        .modelParam('id', model=Folder, level=AccessType.READ)
        .jsonParam('images', 'A list of image ids to include', required=False)
        .pagingParams(defaultSort='lowerName')
        .errorResponse()
        .errorResponse('Read access was denied on the parent folder.', 403)
    )
    @access.public(scope=TokenScope.DATA_READ)
    def folderItemList(self, folder, images, limit, offset, sort):
        import concurrent.futures

        starttime = time.time()
        user = self.getCurrentUser()
        filters = {'largeImage.fileId': {'$exists': True}}
        if isinstance(images, list):
            filters['_id'] = {'$in': [ObjectId(id) for id in images]}
        cursor = Folder().childItems(
            folder=folder, limit=limit, offset=offset, sort=sort,
            filters=filters)
        response = {
            'sort': sort,
            'offset': offset,
            'limit': limit,
            'count': cursor.count(),
            'folder': folder,
            'rootpath': Folder().parentsToRoot(folder, user=user, level=AccessType.READ),
            'large_image_settings': {
                k: Setting().get(k) for k in [
                    getattr(girder_large_image.constants.PluginSettings, key)
                    for key in dir(girder_large_image.constants.PluginSettings)
                    if key.startswith('LARGE_IMAGE_')]},
            'wsi_deid_settings': config.getConfig(),
        }
        with concurrent.futures.ThreadPoolExecutor() as executor:
            response['items'] = [
                item for item in
                executor.map(self._folderItemListGetItem, cursor)
                if item is not None]
        images = {}
        common = None
        allmeta = set()
        for item in response['items']:
            for image in item['images']:
                images[image] = images.get(image, 0) + 1
            common = self._commonValues(common, item['internal_metadata'])
            self._allKeys(allmeta, item['internal_metadata'])
        response['images'] = images
        response['image_names'] = [entry[-1] for entry in sorted(
            [(key != 'label', key != 'macro', key) for key in images.keys()])]
        response['common_internal_metadata'] = common
        response['all_metadata_keys'] = [list(entry) for entry in sorted(allmeta)]
        response['_time'] = time.time() - starttime
        return response

    @autoDescribeRoute(
        Description('Perform an action on a list of items.')
        .jsonParam('ids', 'A list of item ids to redact', required=True)
        # Allow all users to do redaction actions; change to WRITE otherwise
        .param('action', 'Action to perform on the item.  One of process, '
               'reject, quarantine, unquarantine, finish, ocr.', paramType='path',
               enum=['process', 'reject', 'quarantine', 'unquarantine', 'finish', 'ocr'])
        .errorResponse()
        .errorResponse('Write access was denied on the item.', 403)
    )
    @access.user
    def itemListAction(self, ids, action):
        setResponseTimeLimit(86400)
        if not len(ids):
            return
        user = self.getCurrentUser()
        items = [Item().load(id=id, user=user, level=AccessType.READ) for id in ids]
        actionfunc, actionargs, actname, pp = self._actionForItem(items[0], user, action)
        with ItemActionLock:
            ItemActionList.extend(items)
        try:
            with ProgressContext(
                True, user=user, title='%s items' % pp.capitalize(),
                message='%s %s' % (pp.capitalize(), items[0]['name']),
                total=len(ids), current=0
            ) as ctx:
                try:
                    for idx, item in enumerate(items):
                        actionfunc, actionargs, actname, pp = self._actionForItem(
                            item, user, action)
                        ctx.update(
                            message='%s %s' % (pp.capitalize(), item['name']),
                            total=len(ids), current=idx)
                        try:
                            actionfunc(*actionargs)
                        except Exception:
                            logger.exception('Failed to %s item' % actname)
                            ctx.update('Error %s %s' % (pp, item['name']))
                            raise
                    ctx.update(message='Done %s' % pp, total=len(ids), current=len(ids))
                except Exception:
                    pass
        finally:
            with ItemActionLock:
                for item in items:
                    ItemActionList.remove(item)

    @autoDescribeRoute(
        Description('Get the list of known and allowed image names for refiling.')
        .modelParam('id', model=Item, level=AccessType.READ)
        .errorResponse()
    )
    @access.user
    def getRefileList(self, item):
        imageIds = []
        for imageId in item.get('wsi_uploadInfo', {}):
            if not imageId.startswith(TokenOnlyPrefix) and not Item().findOne({
                    'name': {'$regex': '^' + re.escape(imageId) + r'\..*'}}):
                imageIds.append(imageId)
        for imageId in item.get('wsi_uploadInfo', {}):
            if imageId.startswith(TokenOnlyPrefix):
                baseImageId = imageId[len(TokenOnlyPrefix):]
                if baseImageId not in imageIds:
                    imageIds.append(baseImageId)
        return sorted(imageIds)

    @autoDescribeRoute(
        Description('Perform an action on an item.')
        .responseClass('Item')
        # Allow all users to do redaction actions; change to WRITE otherwise
        .modelParam('id', model=Item, level=AccessType.READ)
        .param('imageId', 'The new imageId')
        .param('tokenId', 'The new tokenId', required=False)
        .errorResponse()
        .errorResponse('Write access was denied on the item.', 403)
    )
    @access.user
    def refileItem(self, item, imageId, tokenId):
        folderNameField = config.getConfig('folder_name_field', 'TokenID')
        setResponseTimeLimit(86400)
        user = self.getCurrentUser()
        if imageId and imageId != item['name'].split('.', 1)[0] and Item().findOne({
                'name': {'$regex': '^' + re.escape(imageId) + r'\..*'}}):
            raise RestException('An image with that name already exists.')
        if not imageId:
            imageId = TokenOnlyPrefix + tokenId
        uploadInfo = item.get('wsi_uploadInfo')
        if uploadInfo and TokenOnlyPrefix + imageId in uploadInfo:
            imageId = TokenOnlyPrefix + imageId
        if uploadInfo and imageId in uploadInfo:
            tokenId = uploadInfo[imageId].get(folderNameField, tokenId)
        if not tokenId:
            tokenId = imageId.split('_', 1)[0]
        item = process.refile_image(item, user, tokenId, imageId, uploadInfo)
        return item
