#!/usr/bin/env python3

"""Script for operating on reMarkable tablet ".rm" files."""

import argparse
import datetime
import glob
import json
import subprocess
import sys
import tempfile
import os
import os.path

dirname = os.path.dirname(sys.modules[__name__].__file__)
test_dir = os.path.abspath(dirname)
sys.path.append(test_dir)

import rm2svg
import PyPDF2

# try to add the rmscene repo
VERSION_6_SUPPORT = False
rmscene_dirname = os.path.join(dirname, "..", "..", "rmscene", "src")
rmscene_test_dir = os.path.abspath(rmscene_dirname)
if os.path.isdir(rmscene_test_dir):
    sys.path.append(rmscene_test_dir)
    import rmscene.rm2svg as rm2svgv6
    VERSION_6_SUPPORT = True


ENUM_COMMANDS = ['list', 'convert', 'convert-all']

default_values = {
    'debug': 0,
    'rootdir': None,
    'outdir': None,
    'command': None,
    'infile': None,
    'outfile': None,
}


def read_metadata(rootdir, uuid):
    metadata_file = os.path.join(rootdir, uuid + '.metadata')
    with open(metadata_file, 'r') as f:
        json_text = f.read()
    return json.loads(json_text)


def read_content(rootdir, uuid):
    content_file = os.path.join(rootdir, uuid + '.content')
    with open(content_file, 'r') as f:
        json_text = f.read()
    return json.loads(json_text)


def read_pagedata(rootdir, uuid):
    pagedata_file = os.path.join(rootdir, uuid + '.pagedata')
    with open(pagedata_file, 'r') as f:
        text = f.read()
    return text.split()


class Node:
    def __init__(self, uuid, metadata):
        self.uuid = uuid
        self.metadata = metadata
        self.children = []
        self.next = None

    def __str__(self):
        s = 'uuid: "%s"' % self.uuid
        s += ' metadata: %r' % self.metadata
        s += ' children: %r' % self.children
        s += ' next: %r' % self.next
        return s


# parses the (raw) root directory
def get_repo_info(rootdir, debug):
    # create an unordered node list
    node_list = []
    metadata_list = glob.glob(os.path.join(rootdir, '*.metadata'))
    for metadata_file in metadata_list:
        uuid, _ = os.path.basename(metadata_file).split('.')
        metadata = read_metadata(rootdir, uuid)
        node_list.append([uuid, metadata])

    # add the node list into a tree
    rootnode = Node('', None)
    cur_uuid_list = [rootnode.uuid]
    cur_node = [rootnode]
    prev_node = None

    # define a file to be trashed if it *or its parent* is trashed
    # this "extended" definition prevents an infinite loop due to
    # files that are not trashed, but whose parent is trashed (in the unextended definition)
    def trashed(uuid, metadata):
        if metadata['parent'] == 'trash':
            return True
        for uuid2, metadata2 in node_list:
            if uuid2 == metadata['parent']:
                return trashed(uuid2, metadata2)
        return False

    while node_list:
        new_cur_uuid_list = []
        new_cur_node = []
        new_node_list = []
        for uuid, metadata in node_list:
            if trashed(uuid, metadata):
                # ignore it
                pass
            elif metadata['parent'] in cur_uuid_list:
                # found the parent
                index = cur_uuid_list.index(metadata['parent'])
                node = Node(uuid, metadata)
                cur_node[index].children.append(node)
                new_cur_uuid_list.append(uuid)
                new_cur_node.append(node)
                if prev_node is not None:
                    prev_node.next = node
                prev_node = node
            else:
                # keep looking
                new_node_list.append([uuid, metadata])
        # reset status
        cur_uuid_list = new_cur_uuid_list
        cur_node = new_cur_node
        node_list = new_node_list

    # sort nodes alphabetically (visibleName)
    # using bubble-sort as sizes should be small
    # TODO(chemag): use some interview-acceptable sort here
    def do_bubble_sort(node):
        for indexl in range(len(node.children)):
            for indexr in range(indexl + 1, len(node.children)):
                vall = node.children[indexl].metadata['visibleName']
                valr = node.children[indexr].metadata['visibleName']
                if vall > valr:
                    # swap nodes
                    tmp = node.children[indexl]
                    node.children[indexl] = node.children[indexr]
                    node.children[indexr] = tmp
        # fix next pointers
        for index in range(len(node.children) - 1):
            node.children[index].next = node.children[index + 1]
        if len(node.children) > 0:
            node.children[len(node.children) - 1].next = None

    def node_sort(node):
        # start with the node itself
        do_bubble_sort(node)
        for node in node.children:
            node_sort(node)

    node_sort(rootnode)
    return rootnode


def list_repo(rootdir, debug):
    rootnode = get_repo_info(rootdir, debug)

    # dump lines
    def print_node(node, tab):
        if node.uuid == '':
            # do not print the root node
            pass
        else:
            uuid = node.uuid
            unix_ts = int(node.metadata['lastModified'])
            unix_dt = datetime.datetime.fromtimestamp(unix_ts / 1000)
            unix_str = unix_dt.strftime("%Y%m%d-%H:%M:%S.%f")
            visible_name = node.metadata['visibleName']
            print('%s %s %s %s' % ('  ' * tab, uuid, unix_str, visible_name))
        for node in node.children:
            print_node(node, tab + 1)

    tab = 0
    print_node(rootnode, tab)


def run(command, dry_run, **kwargs):
    env = kwargs.get('env', None)
    stdin = subprocess.PIPE if kwargs.get('stdin', False) else None
    bufsize = kwargs.get('bufsize', 0)
    universal_newlines = kwargs.get('universal_newlines', False)
    default_close_fds = True if sys.platform == 'linux2' else False
    close_fds = kwargs.get('close_fds', default_close_fds)
    shell = type(command) in (type(''), type(u''))
    if dry_run:
        return 0, b'stdout', b'stderr'
    p = subprocess.Popen(command, stdin=stdin, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, bufsize=bufsize,
                         universal_newlines=universal_newlines,
                         env=env, close_fds=close_fds, shell=shell)
    # wait for the command to terminate
    if stdin is not None:
        out, err = p.communicate(stdin)
    else:
        out, err = p.communicate()
    returncode = p.returncode
    # clean up
    del p
    # return results
    return returncode, out, err


def convert_file(infile, outfile, rootdir, debug):
    # get uuid
    uuid = os.path.basename(infile).split('.')[0]
    if not rootdir:
        rootdir = os.path.dirname(infile)
    # get file info
    # metadata = read_metadata(rootdir, uuid)
    content = read_content(rootdir, uuid)
    # pagedata = read_pagedata(rootdir, uuid)
    # create tempdir
    tmpdir = tempfile.mkdtemp(prefix='rmtool.tmp.', dir='/tmp')
    # convert pages to pdf
    pagepdf_list = []

    # get page list
    if content['formatVersion'] == 1:
        page_uuid_list = content['pages']
    elif content['formatVersion'] == 2:
        page_uuid_list = [page['id'] for page in content['cPages']['pages'] if 'deleted' not in page]

    bg_pdf_path = os.path.join(rootdir, uuid + '.pdf')
    bg = PyPDF2.PdfReader(bg_pdf_path) if os.path.exists(bg_pdf_path) else None

    bg_fg = PyPDF2.PdfWriter()

    for page_num, page_uuid in enumerate(page_uuid_list):
        page_path = os.path.join(rootdir, uuid, page_uuid + '.rm')

        # determine whether page has foreground (FG) notes (like a "pure" RM notebook),
        # a background (BG) document (like an annotated PDF file), or both
        fg_page_exists = os.path.exists(page_path)
        if content['formatVersion'] == 1:
            bg_page_exists = os.path.exists(bg_pdf_path) and content['redirectionPageMap'][page_num] >= 0 # TODO: what is redirectionPageMap value for an unannotated page?
        elif content['formatVersion'] == 2:
            bg_page_exists = os.path.exists(bg_pdf_path) and 'redir' in content['cPages']['pages'][page_num]

        # determine document size based on background (BG)
        # if BG does not exist, default to 1404px x 1872px (RM screen size) = 157mm x 210mm (exported PDF)
        # for unexported notes, and scale up with corresponding pixel density later if the notes are extended
        # if BG exists, obtain BG document's physical size and convert it to RM pixels
        px_per_mm_x = 1404 / (445 * 25.4 / 72) # for unextended standard notes
        px_per_mm_y = 1872 / (594 * 25.4 / 72) # for unextended standard notes
        if bg_page_exists:
            if content['formatVersion'] == 1:
                bg_page_num = content['redirectionPageMap'][page_num] # looks like this points to BG PDF page number
            elif content['formatVersion'] == 2:
                bg_page_num = content['cPages']['pages'][page_num]['redir']['value'] # looks like this points to BG PDF page number
            bg_page = bg.pages[bg_page_num]
            bg_width_mm = float(bg_page.mediabox.width) * 25.4 / 72 # mediaBox in user space units (1/72 inch)
            bg_height_mm = float(bg_page.mediabox.height) * 25.4 / 72 # mediaBox in user space units (1/72 inch)
            bg_width_px = bg_width_mm * px_per_mm_x
            bg_height_px = bg_height_mm * px_per_mm_y
        else:
            bg_page = None
            bg_width_px = 1404
            bg_height_px = 1872
            bg_width_mm = bg_width_px / px_per_mm_x
            bg_height_mm = bg_height_px / px_per_mm_y

        # convert foreground (FG) notes to .svg
        pagesvg = os.path.join(tmpdir, page_uuid + '.svg')
        bg_dx_px = 0 # how much to shift background depends on extent of foreground notes
        bg_dy_px = 0
        if fg_page_exists:
            pagerm = page_path

            # determine version
            with open(pagerm, 'rb') as file:
                head_fmt = 'reMarkable .lines file, version=v'
                head = file.read(len(head_fmt)).decode()
                v = head[-1] if head[:-1] == head_fmt[:-1] else None

            try:
                if v in ('6'):
                    # v6 renderer
                    page_info = rm2svgv6.rm2svg(pagerm, pagesvg, minwidth=bg_width_px, minheight=bg_height_px)
                    fg_width_px = page_info.width
                    fg_height_px = page_info.height
                    bg_dx_px = page_info.xpos_delta - bg_width_px/2 # see rmscene rm2svg.py: get_dimensions()
                    bg_dy_px = page_info.ypos_delta
                elif v in ('1', '2', '3', '4', '5'):
                    # pre-v6 renderer
                    rm2svg.rm2svg(pagerm, pagesvg, True, bg_width_px, bg_height_px)
                    fg_width_px = bg_width_px
                    fg_height_px = bg_height_px
                else:
                    raise 'Unknown reMarkable .lines version: {v}'

                fg_width_mm = fg_width_px / px_per_mm_x
                fg_height_mm = fg_height_px / px_per_mm_y

                # convert FG from .svg to .pdf
                # TODO: revert to inkscape, now that I'm using PyPDF2 to merge anyway (but need to handle width)?
                pagepdf = os.path.join(tmpdir, page_uuid + '.pdf')
                command = 'rsvg-convert --format=pdf --width=%fmm --height=%fmm "%s" > "%s"' % (fg_width_mm, fg_height_mm, pagesvg, pagepdf)
                returncode, out, err = run(command, False)
                assert(returncode == 0), command
            except Exception as e:
                print(f'WARNING: could not render foreground - skipping it! Exception:\n{str(e)}')
                fg_page_exists = False # ensure no attempt is made to render it later

        # if foreground does not exist (or it failed to render above),
        # then pretend it has the same size as the background
        if not fg_page_exists:
            fg_width_px = bg_width_px
            fg_height_px = bg_height_px
            fg_width_mm = fg_width_px / px_per_mm_x
            fg_height_mm = fg_height_px / px_per_mm_y

        if debug > 0:
            print(f'....', end='') # indent
            print(f'page {page_num+1}/{len(page_uuid_list)}', end=', ') # page number
            print('+'.join((['BG'] if bg_page_exists else []) + (['FG'] if fg_page_exists else [])), end=', ') # "BG" / "FG" / "BG+FG"
            print(f'{fg_width_px:.1f}px x {fg_height_px:.1f}px = {fg_width_mm:.1f}mm x {fg_height_mm:.1f}mm.') # size

        # TODO: can probably optimize rendering!

        # 1) begin with blank page
        bg_fg_page = PyPDF2.PageObject.create_blank_page(width=fg_width_mm * 72 / 25.4, height=fg_height_mm * 72 / 25.4)

        # 2) draw background if it exists
        if bg_page_exists:
            bg_dx_mm = bg_dx_px / px_per_mm_x
            bg_dy_mm = bg_dy_px / px_per_mm_y
            trans = PyPDF2.Transformation().translate(tx=bg_dx_mm * 72 / 25.4, ty=bg_dy_mm * 72 / 25.4)
            bg_fg_page.merge_page(bg_page)
            bg_fg_page.add_transformation(trans)

        # 3) draw foreground if it exists
        if fg_page_exists:
            fg_page = PyPDF2.PdfReader(pagepdf).pages[0]
            bg_fg_page.merge_page(fg_page)

        # 4) add page to PDF
        bg_fg.add_page(bg_fg_page)

    # finally, write PDF from memory to file
    with open(outfile, "wb") as file:
        bg_fg.write(file)


def convert_all(rootdir, outdir, debug):
    rootnode = get_repo_info(rootdir, debug)

    # traverse the tree
    def traverse_node(node, rootdir, outdir, debug):
        if node.uuid == '':
            # ignore the root node
            pass
        else:
            uuid = node.uuid
            unix_ts_ms = int(node.metadata['lastModified'])
            unix_ts = unix_ts_ms / 1000
            visible_name = node.metadata['visibleName']
            visible_name = visible_name.replace(' ', '_')
            t = node.metadata['type']
            if t == 'CollectionType':
                # folder: make sure the directory exists
                outdir = os.path.join(outdir, visible_name)
                if not os.path.exists(outdir):
                    os.mkdir(outdir, mode=0o755)
            elif t == 'DocumentType':
                # file: convert the file into a pdf
                # check whether the file already exists (with right timestamp)
                infile = os.path.join(rootdir, uuid)
                outfile = os.path.join(outdir, visible_name + '.pdf')
                do_convert = True
                if os.path.exists(outfile):
                    # output file already exists: check mtime
                    file_mtime = os.stat(outfile).st_mtime
                    if file_mtime > unix_ts:
                        # output file exists and is younger
                        do_convert = False
                if do_convert:
                    if debug > 0:
                        print('..converting %s -> %s' % (infile, outfile))
                    convert_file(infile, outfile, rootdir, debug)
        for node in node.children:
            traverse_node(node, rootdir, outdir, debug)

    traverse_node(rootnode, rootdir, outdir, debug)


def get_options(argv):
    """Generic option parser.

    Args:
        argv: list containing arguments

    Returns:
        Namespace - An argparse.ArgumentParser-generated option object
    """
    # init parser
    # usage = 'usage: %prog [options] arg1 arg2'
    # parser = argparse.OptionParser(usage=usage)
    # parser.print_help() to get argparse.usage (large help)
    # parser.print_usage() to get argparse.usage (just usage line)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
            '-d', '--debug', action='count',
            dest='debug', default=default_values['debug'],
            help='Increase verbosity (use multiple times for more)',)
    parser.add_argument(
            '--quiet', action='store_const',
            dest='debug', const=-1,
            help='Zero verbosity',)
    parser.add_argument(
            '-r', '--root', action='store', type=str,
            dest='rootdir', default=default_values['rootdir'],
            metavar='ROOT',
            help='use ROOT as root directory (xochitl)',)
    parser.add_argument(
            '-o', '--outdir', action='store', type=str,
            dest='outdir', default=default_values['outdir'],
            metavar='OUTDIR',
            help='use OUTDIR as outdir directory',)
    # add command
    parser.add_argument(
            'command', action='store', type=str,
            default=default_values['command'],
            choices=ENUM_COMMANDS,
            metavar='[%s]' % (' | '.join(ENUM_COMMANDS,)),
            help='command',)
    # add i/o
    parser.add_argument(
            'infile', type=str, nargs='?',
            default=default_values['infile'],
            metavar='input-file',
            help='input file',)
    parser.add_argument(
            'outfile', type=str, nargs='?',
            default=default_values['outfile'],
            metavar='output-file',
            help='output file',)
    # do the parsing
    options = parser.parse_args(argv[1:])
    return options


def main(argv):
    # parse options
    options = get_options(argv)
    # print results
    if options.debug > 0:
        print(options)
    # need a valid infile or root directory

    # do something
    if options.command == 'list':
        list_repo(options.rootdir, options.debug)
    elif options.command == 'convert':
        convert_file(options.infile, options.outfile, options.rootdir, options.debug)
    elif options.command == 'convert-all':
        convert_all(options.rootdir, options.outdir, options.debug)


if __name__ == '__main__':
    # at least the CLI program name: (CLI) execution
    main(sys.argv)
