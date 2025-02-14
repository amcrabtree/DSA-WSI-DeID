import $ from 'jquery';
import _ from 'underscore';
import events from '@girder/core/events';
import { getCurrentUser } from '@girder/core/auth';
import { restRequest } from '@girder/core/rest';
import router from '@girder/core/router';
import { wrap } from '@girder/core/utilities/PluginUtils';
import HierarchyWidget from '@girder/core/views/widgets/HierarchyWidget';
import { formatCount } from '@girder/core/misc';

import { goToNextUnprocessedFolder } from '../utils';
import '../stylesheets/HierarchyWidget.styl';

function performAction(action) {
    const actions = {
        ingest: { done: 'Import completed.', fail: 'Failed to import.' },
        export: { done: 'Recent export task completed.', fail: 'Failed to export recent items.  Check export file location for disk drive space or other system issues.' },
        exportall: { done: 'Export all task completed.', fail: 'Failed to export all items.  Check export file location for disk drive space or other system issues.' },
        ocrall: { done: 'Started background job to find label text on WSIs in this folder.', fail: 'Failed to start background task to find label text for images.' }
    };

    let data = {};
    if (action.startsWith('list/')) {
        if (this.itemListView && this.itemListView.checked && this.itemListView._wsi_deid_item_list) {
            let ids = this.itemListView.checked.map((cid) => this.itemListView.collection.get(cid).id);
            ids = ids.filter((id) => this.itemListView._wsi_deid_item_list.byId[id]);
            if (!ids.length) {
                return;
            }
            data.ids = JSON.stringify(ids);
        } else {
            return;
        }
    }

    restRequest({
        method: 'PUT',
        url: `wsi_deid/action/${action}`,
        data: data,
        error: null
    }).done((resp) => {
        if (!actions[action]) {
            return;
        }
        let text = actions[action].done;
        let any = false;
        if (resp.action === 'ingest') {
            if (['duplicate', 'missing', 'unlisted', 'failed', 'notexcel', 'badformat', 'badentry'].some((key) => resp[key])) {
                text = 'Import process completed with errors.';
            }
            [
                ['added', 'added'],
                ['present', 'already present'],
                ['replaced', 'replaced'],
                ['duplicate', 'with duplicate ImageID.  Check DeID Upload file'],
                ['missing', 'missing from import folder.  Check DeID Upload File and WSI image filenames'],
                ['badentry', 'with invalid data in a DeID Upload File'],
                ['unlisted', 'in the import folder, but not listed in a DeID Upload Excel/CSV file'],
                ['failed', 'failed to import.  Check if image file(s) are in an accepted WSI format'],
                ['unfiled', 'not yet associated with a row from the import sheet. Uploaded to the Unfiled folder']
            ].forEach(([key, desc]) => {
                if (resp[key]) {
                    if (key === 'unlisted' && !resp.parsed && !resp.notexcel && !resp.badformat) {
                        text += '  No DeID Upload file present.';
                    } else {
                        text += `  ${resp[key]} image${resp[key] > 1 ? 's' : ''} ${desc}.`;
                    }
                    any = true;
                }
            });
            [
                ['parsed', 'parsed'],
                ['notexcel', 'could not be read'],
                ['badformat', 'incorrectly formatted']
            ].forEach(([key, desc]) => {
                if (resp[key]) {
                    text += `  ${resp[key]} DeID Upload Excel file${resp[key] > 1 ? 's' : ''} ${desc}.`;
                    any = true;
                }
            });
            if (!any) {
                text = 'Nothing to import.  Import folder is empty.';
            }
        }
        if (resp.action === 'export' || resp.action === 'exportall') {
            if (resp['local_export_enabled']) {
                [
                    ['finished', 'exported'],
                    ['present', 'previously exported and already exist(s) in export folder'],
                    ['different', 'with the same ImageID but different WSI file size already present in export folder. Remove the corresponding image(s) from the export directory and select Export again'],
                    ['quarantined', 'currently quarantined.  Only files in "Approved" workflow stage are transferred to export folder'],
                    ['rejected', 'with rejected status.  Only files in "Approved" workflow stage are transferred to export folder']
                ].forEach(([key, desc]) => {
                    if (resp[key]) {
                        text += `  ${resp[key]} image${resp[key] > 1 ? 's' : ''} ${desc}.`;
                        any = true;
                    }
                });
                if (!any) {
                    text = 'Nothing to export.';
                }
            }
        }
        if (resp.action === 'ocrall') {
            if (resp.ocrJobId) {
                events.once('g:alert', () => {
                    $('#g-alerts-container:last div.alert:last').append($('<span> </span>')).append($('<a/>').text('Track its progress here.').attr('href', `/#job/${resp.ocrJobId}`));
                }, this);
            } else {
                text = 'No new items without existing label text metadata.';
            }
        }
        if (resp.fileId) {
            events.once('g:alert', () => {
                $('#g-alerts-container:last div.alert:last').append($('<span> </span>')).append($('<a/>').text('See the Excel report for more details.').attr('href', `/api/v1/file/${resp.fileId}/download`));
            }, this);
        }
        if (resp['sftp_enabled']) {
            const message = ' Transfer of files to remote server via SFTP started in background.';
            let sftpAlertInfo = $(`<span>${message} </span>`);
            if (resp.sftp_job_id) {
                sftpAlertInfo.append($('<a/>').text('Track remote transfer here.').attr('href', `/#job/${resp.sftp_job_id}`));
            }
            events.once('g:alert', () => {
                $('#g-alerts-container:last div.alert:last').append(sftpAlertInfo);
            }, this);
        }
        if (resp.action === 'ingest') {
            // If import launches an ocr batch job, add that info at the very end of the alert
            [
                { id: resp['ocr_job'], message: ' Started background job to find label text on WSIs in this folder.' },
                { id: resp['unfiled_job'], message: ' Started background job to match unfiled images with upload data.' }
            ].forEach((jobInfo) => {
                if (jobInfo.id) {
                    let jobAlertInfo = $(`<span>${jobInfo.message} </span>`).append($('<a/>').text('Track its progress here.').attr('href', `/#job/${jobInfo.id}`));
                    events.once('g:alert', () => {
                        $('#g-alerts-container:last div.alert:last').append(jobAlertInfo);
                    }, this);
                }
            });
        }
        events.trigger('g:alert', {
            icon: 'ok',
            text: text,
            type: 'success',
            timeout: 0
        });
        if (resp.action === 'ingest' && this.parentModel.get('_modelType') === 'folder') {
            router.navigate('folder/' + this.parentModel.id + '?_=' + Date.now(), { trigger: true });
        }
    }).fail((resp) => {
        let text = actions[action].fail;
        /*
        if (resp.responseJSON && resp.responseJSON.message) {
            text += ' ' + resp.responseJSON.message;
        }
        */
        events.trigger('g:alert', {
            icon: 'cancel',
            text: text,
            type: 'danger',
            timeout: 0
        });
    });
    if (action.startsWith('list/')) {
        goToNextUnprocessedFolder(undefined, this.parentModel.id);
    }
}

function addControls(key, settings) {
    const controls = {
        ingest: [
            {
                key: 'redactlist',
                text: 'Redact Checked',
                class: 'btn-info disabled',
                action: 'list/process',
                check: () => settings.show_metadata_in_lists
            }, {
                key: 'import',
                text: 'Import',
                class: 'btn-info',
                action: 'ingest',
                check: () => settings.show_import_button !== false
            }, {
                key: 'ocr',
                text: 'Find label text',
                class: 'btn-info',
                action: 'ocrall',
                check: _.constant(true)
            }
        ],
        finished: [
            {
                key: 'export',
                text: 'Export Recent',
                class: 'btn-info',
                action: 'export',
                check: () => settings.show_export_button !== false
            }, {
                key: 'exportall',
                text: 'Export All',
                class: 'btn-info',
                action: 'exportall',
                check: () => settings.show_export_button !== false
            }
        ],
        processed: [
            {
                key: 'finishlist',
                text: 'Approve Checked',
                class: 'btn-info disabled',
                action: 'list/finish',
                check: () => settings.show_metadata_in_lists
            }
        ],
        quarantine: [
            {
                key: 'redactlist',
                text: 'Redact Checked',
                class: 'btn-info disabled',
                action: 'list/process',
                check: () => settings.show_metadata_in_lists
            }
        ]
    };
    if (!controls[key]) {
        return;
    }
    var btns = this.$el.find('.g-hierarchy-actions-header .g-folder-header-buttons');
    for (let i = controls[key].length - 1; i >= 0; i -= 1) {
        let control = controls[key][i];
        if (control.check()) {
            btns.prepend(`<button class="wsi_deid-${control.key}-button btn ${control.class}">${control.text}</button>`);
            this.events[`click .wsi_deid-${control.key}-button`] = () => { performAction.call(this, control.action); };
        }
    }
    this.delegateEvents();
}

wrap(HierarchyWidget, 'render', function (render) {
    render.call(this);

    if (this.parentModel.resourceName === 'folder' && getCurrentUser()) {
        restRequest({
            url: `wsi_deid/project_folder/${this.parentModel.id}`,
            error: null
        }).done((resp) => {
            if (resp) {
                restRequest({
                    url: `wsi_deid/settings`,
                    error: null
                }).done((settings) => {
                    addControls.call(this, resp, settings);
                });
            }
        });
    }
    return this;
});

wrap(HierarchyWidget, 'fetchAndShowChildCount', function (fetchAndShowChildCount) {
    let showSubtreeCounts = () => {
        const folders = this.parentModel.get('nFolders') || 0;
        const items = this.parentModel.get('nItems') || 0;
        const subfolders = Math.max(folders, this.parentModel.get('nSubtreeCount').folders - 1 || 0);
        const subitems = Math.max(items, this.parentModel.get('nSubtreeCount').items || 0);
        let folderCount = formatCount(folders) + (subfolders > folders ? (' (' + formatCount(subfolders) + ')') : '');
        let itemCount = formatCount(items) + (subitems > items ? (' (' + formatCount(subitems) + ')') : '');
        let folderTooltip = `${folders} folder${folders === 1 ? '' : 's'}`;
        if (folders < subfolders) {
            folderTooltip += ` (current folder), ${subfolders} total folder${subfolders === 1 ? '' : 's'} (including subfolders)`;
        } else if (folders) {
            folderTooltip += ' (all in current folder)';
        }
        let itemTooltip = `${items} file${items === 1 ? '' : 's'}`;
        if (items < subitems) {
            itemTooltip += ` (current folder), ${subitems} total file${subitems === 1 ? '' : 's'} (including in subfolders)`;
        } else if (items) {
            itemTooltip += ' (all in current folder)';
        }
        if (!this.$('.g-item-count').length) {
            this.$('.g-subfolder-count-container').after($('<div class="g-item-count-container"><i class="icon-doc-text-inv"></i><div class="g-item-count"></div></div>'));
        }
        this.$('.g-subfolder-count').text(folderCount);
        this.$('.g-item-count').text(itemCount);
        this.$('.g-subfolder-count-container').attr('title', folderTooltip);
        this.$('.g-item-count-container').attr('title', itemTooltip);
        this.$('.g-subfolder-count-container').toggleClass('subtree-info', subfolders > folders);
        this.$('.g-item-count-container').toggleClass('subtree-info', subitems > items);
    };

    let reshowSubtreeCounts = () => {
        restRequest({
            url: `wsi_deid/resource/${this.parentModel.id}/subtreeCount`,
            data: { type: this.parentModel.get('_modelType') }
        }).done((data) => {
            this.parentModel.set('nSubtreeCount', data);
            showSubtreeCounts();
        });
    };

    fetchAndShowChildCount.call(this);
    if (this.parentModel.has('nSubtreeCount')) {
        showSubtreeCounts();
    } else {
        this.parentModel.set('nSubtreeCount', {}); // prevents fetching details twice
        reshowSubtreeCounts();
    }
    this.parentModel.off('change:nItems', reshowSubtreeCounts, this)
        .on('change:nItems', reshowSubtreeCounts, this)
        .off('change:nFolders', reshowSubtreeCounts, this)
        .on('change:nFolders', reshowSubtreeCounts, this);
    return this;
});
