# These are the Mailpile commands, the public "API" we expose for searching,
# tagging and editing e-mail.
#
import datetime
import json
import os
import os.path
import re
import traceback

import mailpile.util
import mailpile.ui
from mailpile.mailutils import ExtractEmails, NotEditableError, IsMailbox
from mailpile.mailutils import Email, NoFromAddressError, PrepareMail, SendMail
from mailpile.postinglist import GlobalPostingList
from mailpile.search import MailIndex
from mailpile.util import *

class Command:
    """Generic command object all others inherit from"""
    SYNOPSIS = (None,    # CLI shortcode, e.g. A:
                None,    # CLI shortname, e.g. add
                None,    # API endpoint, e.g. sys/addmailbox
                None)    # Positional argument list
    EXAMPLES = None
    FAILURE = 'Failed: %(name)s %(args)s'
    ORDER = (None, 0)
    SERIALIZE = False
    SPLIT_ARG = 10000  # A big number!
    RAISES = (UsageError, UrlRedirectException)

    HTTP_CALLABLE = ('GET', )
    HTTP_POST_VARS = {}
    HTTP_QUERY_VARS = {}
    HTTP_BANNED_VARS = {}
    HTTP_STRICT_VARS = True

    class CommandResult:
        def __init__(self, session, command, template_id, doc, result,
                           status, message, args=[], kwargs={}):
            self.session = session
            self.command = command
            self.args = args
            self.kwargs = kwargs
            self.template_id = template_id
            self.doc = doc
            self.result = result
            self.status = status
            self.message = message

        def __nonzero__(self):
            return (self.result and True or False)

        def as_text(self):
            if isinstance(self.result, bool):
                return '%s: %s' % (self.result and 'Succeeded' or 'Failed',
                                   self.doc)
            return unicode(self.result)

        __str__ = lambda self: self.as_text()

        __unicode__ = lambda self: self.as_text()

        def as_dict(self):
            rv = {
                'command': self.command,
                'status': self.status,
                'message': self.message,
                'result': self.result,
                'elapsed': '%.3f' % self.session.ui.time_elapsed,
            }
            for ui_key in [k for k in self.kwargs.keys()
                                   if k.startswith('ui_')]:
                rv[ui_key] = self.kwargs[ui_key]
            return rv

        def as_html(self, template=None):
            path_parts = (self.template_id or 'command').split('/')
            if len(path_parts) == 1:
                path_parts.append('index')
            if template not in (None, 'html', 'as.html'):
                # Security: The template request may come from the URL, so we
                #           sanitize it very aggressively before heading off to
                #           the filesystem.
                clean_tpl = CleanText(template.replace('.html', ''),
                                      banned=CleanText.FS +
                                             CleanText.WHITESPACE)
                path_parts[-1] += '-%s' % clean_tpl
            tpath = os.path.join(*path_parts)
            return self.session.ui.render_html(self.session.config, [tpath],
                                               self.as_dict())

        def as_json(self):
            return self.session.ui.render_json(self.as_dict())

    def __init__(self, session, name=None, arg=None, data=None):
        self.session = session
        self.serialize = self.SERIALIZE
        self.name = name
        self.data = data or {}
        self.status = 'success'
        self.message = 'OK'
        self.result = None
        if type(arg) in (type(list()), type(tuple())):
            self.args = list(arg)
        elif arg:
            if self.SPLIT_ARG:
                self.args = arg.split(' ', self.SPLIT_ARG)
            else:
                self.args = [arg]
        else:
            self.args = []

    def _idx(self, reset=False, wait=True, quiet=False):
        session, config = self.session, self.session.config
        if not reset and config.index:
            return config.index

        def __do_load():
            if reset:
                config.index = None
                session.results = []
                session.searched = []
                session.displayed = {'start': 1, 'count': 0}
            idx = config.get_index(session)
            idx.update_tag_stats(session, config)
            if not wait:
                session.ui.reset_marks(quiet=quiet)
            return idx
        if wait:
            return config.slow_worker.do(session, 'Load', __do_load)
        else:
            config.slow_worker.add_task(session, 'Load', __do_load)
            return None

    def _choose_messages(self, words):
        msg_ids = set()
        all_words = []
        for word in words:
            all_words.extend(word.split(','))
        for what in all_words:
            if what.lower() == 'these':
                b = self.session.displayed['start'] - 1
                c = self.session.displayed['count']
                msg_ids |= set(self.session.results[b:b + c])
            elif what.lower() == 'all':
                msg_ids |= set(self.session.results)
            elif what.startswith('='):
                try:
                    msg_id = int(what[1:], 36)
                    if msg_id >= 0 and msg_id < len(self._idx().INDEX):
                        msg_ids.add(msg_id)
                    else:
                        self.session.ui.warning((_('ID out of bounds: %s')
                                                 ) % (what[1:], ))
                except ValueError:
                    self.session.ui.warning(_('What message is %s?') % (what, ))
            elif '-' in what:
                try:
                    b, e = what.split('-')
                    msg_ids |= set(self.session.results[int(b) - 1:int(e)])
                except:
                    self.session.ui.warning(_('What message is %s?') % (what, ))
            else:
                try:
                    msg_ids.add(self.session.results[int(what) - 1])
                except:
                    self.session.ui.warning(_('What message is %s?') % (what, ))
        return msg_ids

    def _error(self, message):
        self.status = 'error'
        self.message = message
        self.session.ui.error(message)
        return False

    def _read_file_or_data(self, fn):
        if fn in self.data:
            return self.data[fn]
        else:
            return open(fn, 'rb').read()

    def _ignore_exception(self):
        self.session.ui.debug(traceback.format_exc())

    def _serialize(self, name, function):
        session, config = self.session, self.session.config
        return config.slow_worker.do(session, name, function)

    def _background(self, name, function):
        session, config = self.session, self.session.config
        return config.slow_worker.add_task(session, name, function)

    def _starting(self):
        if self.name:
            self.session.ui.start_command(self.name, self.args, self.data)

    def _finishing(self, command, rv):
        if self.name:
            self.session.ui.finish_command()
        result = self.CommandResult(self.session, self.name, self.SYNOPSIS[2],
                                    command.__doc__ or self.__doc__,
                                    rv, self.status, self.message,
                                    args=self.args, kwargs=self.data)
        return result

    def _run(self, *args, **kwargs):
        def command(self, *args, **kwargs):
            return self.command(*args, **kwargs)
        try:
            self._starting()
            return self._finishing(command, command(self, *args, **kwargs))
        except self.RAISES:
            raise
        except:
            self._ignore_exception()
            self._error(self.FAILURE % {'name': self.name,
                                        'args': ' '.join(self.args)})
            return self._finishing(command, False)

    def run(self, *args, **kwargs):
        if self.serialize:
            # Some functions we always run in the slow worker, to make sure
            # they don't get run in parallel with other things.
            return self._serialize(self.serialize,
                                   lambda: self._run(*args, **kwargs))
        else:
            return self._run(*args, **kwargs)

    def command(self):
        return None


##[ Internals ]###############################################################

class Load(Command):
    """Load or reload the metadata index"""
    SYNOPSIS = (None, 'load', None, None)
    ORDER = ('Internals', 1)

    def command(self, reset=True, wait=True, quiet=False):
        return self._idx(reset=reset, wait=wait, quiet=quiet) and True or False


class Rescan(Command):
    """Add new messages to index"""
    SYNOPSIS = (None, 'rescan', None, '[all|vcards|<msgs>]')
    ORDER = ('Internals', 2)
    SERIALIZE = 'Rescan'

    def command(self):
        session, config = self.session, self.session.config

        delay = play_nice_with_threads()
        if delay > 0:
            session.ui.notify((
                _('Note: periodic delay is %ss, run from shell to '
                  'speed up: mp --rescan=...')
            ) % delay)

        if self.args and self.args[0].lower() == 'vcards':
            return self._rescan_vcards(session, config)
        elif self.args and self.args[0].lower() == 'all':
            self.args.pop(0)

        msg_idxs = self._choose_messages(self.args)
        if msg_idxs:
            session.ui.warning(_('FIXME: rescan messages: %s') % msg_idxs)
            return False
        else:
            # FIXME: Need a lock here?
            if 'rescan' in config._running:
                return True
            config._running['rescan'] = True
            try:
                return dict_merge(
                    self._rescan_vcards(session, config),
                    self._rescan_mailboxes(session, config)
                )
            finally:
                del config._running['rescan']

    def _rescan_vcards(self, session, config):
        import mailpile.plugins
        imported = 0
        importer_cfgs = config.prefs.vcard.importers
        for importer in mailpile.plugins.VCARD_IMPORTERS.values():
            for cfg in importer_cfgs.get(importer.SHORT_NAME, []):
                imported += importer(session, cfg
                                     ).import_vcards(session, config.vcards)
        return {'vcards': imported}

    def _rescan_mailboxes(self, session, config):
        idx = self._idx()
        msg_count = 0
        mbox_count = 0
        rv = True
        try:
            pre_command = config.prefs.rescan_command
            if pre_command:
                session.ui.mark(_('Running: %s') % pre_command)
                subprocess.check_call(pre_command, shell=True)
            msg_count = 1
            for fid, fpath in config.get_mailboxes():
                if fpath == '/dev/null':
                    continue
                if mailpile.util.QUITTING:
                    break
                count = idx.scan_mailbox(session, fid, fpath,
                                         config.open_mailbox)
                if count:
                    msg_count += count
                    mbox_count += 1
                config.clear_mbox_cache()
                session.ui.mark('\n')
            msg_count -= 1
            if msg_count:
                idx.cache_sort_orders(session)
                if not mailpile.util.QUITTING:
                    GlobalPostingList.Optimize(session, idx, quick=True)
            else:
                session.ui.mark(_('Nothing changed'))
        except (KeyboardInterrupt, subprocess.CalledProcessError) as e:
            session.ui.mark(_('Aborted: %s') % e)
            self._ignore_exception()
            return {'aborted': True,
                    'messages': msg_count,
                    'mailboxes': mbox_count}
        finally:
            if msg_count:
                session.ui.mark('\n')
                if msg_count < 500:
                    idx.save_changes(session)
                else:
                    idx.save(session)
            idx.update_tag_stats(session, config)
        return {'messages': msg_count,
                'mailboxes': mbox_count}


class Optimize(Command):
    """Optimize the keyword search index"""
    SYNOPSIS = (None, 'optimize', None, '[harder]')
    ORDER = ('Internals', 3)
    SERIALIZE = 'Optimize'

    def command(self):
        try:
            self._idx().save(self.session)
            GlobalPostingList.Optimize(self.session, self._idx(),
                                       force=('harder' in self.args))
            return True
        except KeyboardInterrupt:
            self.session.ui.mark(_('Aborted'))
            return False


class UpdateStats(Command):
    """Force statistics update"""
    SYNOPSIS = (None, 'recount', None, None)
    ORDER = ('Internals', 4)

    def command(self):
        session, config = self.session, self.session.config
        idx = config.index
        if 'tags' in config:
            idx.update_tag_stats(session, config, config.tags.keys())
            session.ui.mark(_("Statistics updated"))
            return True
        else:
            return False


class RunWWW(Command):
    """Just run the web server"""
    SYNOPSIS = (None, 'www', None, None)
    ORDER = ('Internals', 5)

    def command(self):
        self.session.config.prepare_workers(self.session, daemons=True)
        while not mailpile.util.QUITTING:
            time.sleep(1)
        return True


class RenderPage(Command):
    """Does nothing, for use by semi-static jinja2 pages"""
    SYNOPSIS = (None, None, 'page', None)
    ORDER = ('Internals', 6)
    SPLIT_ARG = False
    HTTP_STRICT_VARS = False

    class CommandResult(Command.CommandResult):
        def __init__(self, *args, **kwargs):
            Command.CommandResult.__init__(self, *args, **kwargs)
            if self.result and 'path' in self.result:
                self.template_id += '/' + self.result['path'] + '/index'

    def command(self):
        return {
            'path': (self.args and self.args[0] or ''),
            'data': self.data
        }


##[ Configuration commands ]###################################################

class ConfigSet(Command):
    """Change a setting"""
    SYNOPSIS = ('S', 'set', 'settings/set', '<section.variable> <value>')
    ORDER = ('Config', 1)
    SPLIT_ARG = False
    HTTP_CALLABLE = ('POST', 'UPDATE')
    HTTP_STRICT_VARS = False
    HTTP_POST_VARS = {
       'section.variable': 'value|json-string',
    }

    def command(self):
        config = self.session.config
        args = self.args[:]
        ops = []

        for var in self.data.keys():
            parts = ('.' in var) and var.split('.') or var.split('/')
            if parts[0] in config.rules:
                ops.append((var, self.data[var]))

        if self.args:
            arg = ' '.join(self.args)
            if '=' in arg:
                # Backwards compatiblity with the old 'var = value' syntax.
                var, value = [s.strip() for s in arg.split('=', 1)]
                var = var.replace(': ', '.').replace(':', '.').replace(' ', '')
            else:
                var, value = arg.split(' ', 1)
            ops.append((var, value))

        for path, value in ops:
            value = value.strip()
            if value.startswith('{') or value.startswith('['):
                value = json.loads(value)
            try:
                cfg, var = config.walk(path.strip(), parent=1)
                cfg[var] = value
            except IndexError:
                cfg, v1, v2 = config.walk(path.strip(), parent=2)
                cfg[v1] = {v2: value}

        self._serialize('Save config', lambda: config.save())
        return True


class ConfigAdd(Command):
    """Add a new value to a list (or ordered dict) setting"""
    SYNOPSIS = ('S', 'append', 'settings/add', '<section.variable> <value>')
    ORDER = ('Config', 1)
    SPLIT_ARG = False
    HTTP_CALLABLE = ('POST', 'UPDATE')
    HTTP_STRICT_VARS = False
    HTTP_POST_VARS = {
        'section.variable': 'value|json-string',
    }

    def command(self):
        config = self.session.config
        args = self.args[:]
        ops = []

        for var in self.data.keys():
            parts = ('.' in var) and var.split('.') or var.split('/')
            if parts[0] in config.rules:
                ops.append((var, self.data[var][0]))

        if self.args:
            arg = ' '.join(self.args)
            if '=' in arg:
                # Backwards compatible with the old 'var = value' syntax.
                var, value = [s.strip() for s in arg.split('=', 1)]
                var = var.replace(': ', '.').replace(':', '.').replace(' ', '')
            else:
                var, value = arg.split(' ', 1)
            ops.append((var, value))

        for path, value in ops:
            value = value.strip()
            if value.startswith('{') or value.startswith('['):
                value = json.loads(value)
            cfg, var = config.walk(path.strip(), parent=1)
            cfg[var].append(value)

        self._serialize('Save config', lambda: config.save())
        return True


class ConfigUnset(Command):
    """Reset one or more settings to their defaults"""
    SYNOPSIS = ('U', 'unset', 'settings/unset', '<var>')
    ORDER = ('Config', 2)
    HTTP_CALLABLE = ('POST', )
    HTTP_POST_VARS = {
        'var': 'section.variables'
    }

    def command(self):
        session, config = self.session, self.session.config
        vlist = self.args[:]
        vlist.extend(self.data.get('var', None) or [])
        for v in vlist:
            cfg, vn = config.walk(v, parent=True)
            if vn in cfg:
                del cfg[vn]
        self._serialize('Save config', lambda: config.save())
        return True


class ConfigPrint(Command):
    """Print one or more settings"""
    SYNOPSIS = ('P', 'print', 'settings', '<var>')
    ORDER = ('Config', 3)
    SPLIT_ARG = False
    HTTP_QUERY_VARS = {
        'var': 'section.variable'
    }

    def command(self):
        session, config = self.session, self.session.config
        result = {}
        try:
            # FIXME: Are there privacy implications here somewhere?
            for key in (self.args + self.data.get('var', [])):
                result[key] = config.walk(key)
        except KeyError:
            session.ui.error(_('No such key: %s') % key)
            return False
        return result


class AddMailboxes(Command):
    """Add one or more mailboxes"""
    SYNOPSIS = ('A', 'add', None, '<path/to/mailbox>')
    ORDER = ('Config', 4)
    SPLIT_ARG = False
    HTTP_CALLABLE = ('POST', 'UPDATE')

    def command(self):
        session, config = self.session, self.session.config
        adding = []
        existing = config.sys.mailbox
        paths = self.args[:]
        while paths:
            raw_fn = paths.pop(0)
            fn = os.path.abspath(os.path.normpath(os.path.expanduser(raw_fn)))
            if raw_fn in existing or fn in existing:
                session.ui.warning('Already in the pile: %s' % raw_fn)
            elif raw_fn.startswith("imap://"):
                adding.append(raw_fn)
            elif os.path.exists(fn):
                if IsMailbox(fn):
                    adding.append(fn)
                elif os.path.isdir(fn):
                    session.ui.mark('Scanning %s for mailboxes' % fn)
                    for f in [f for f in os.listdir(fn)
                                      if not f.startswith('.')]:
                        paths.append(os.path.join(fn, f))
            else:
                return self._error('No such file/directory: %s' % raw_fn)

        added = {}
        for arg in adding:
            added[config.sys.mailbox.append(arg)] = arg
        if added:
            self._serialize('Save config', lambda: config.save())
            return {'added': added}
        else:
            return True


###############################################################################

class Output(Command):
    """Choose format for command results."""
    SYNOPSIS = (None, 'output', None, '[json|text|html|<template>.html|...]')
    ORDER = ('Internals', 7)
    HTTP_STRICT_VARS = False

    def command(self):
        self.session.ui.render_mode = self.args and self.args[0] or 'text'
        return {'output': self.session.ui.render_mode}


class Help(Command):
    """Print help on Mailpile or individual commands."""
    SYNOPSIS = ('h', 'help', 'help', '[<command-group>|variables]')
    ABOUT = ('This is Mailpile!')
    ORDER = ('Config', 9)

    class CommandResult(Command.CommandResult):

        def splash_as_text(self):
            return '\n'.join([
                self.result['splash'],
                _('The web interface is %s') % (self.result['http_url'] or
                                             _('disabled.')),
                '',
                _('For instructions, type `help`, press <CTRL-D> to quit.'),
                ''
            ])

        def variables_as_text(self):
            text = []
            for group in self.result['variables']:
                text.append(group['name'])
                for var in group['variables']:
                    sep = ('=' in var['type']) and ': ' or ' = '
                    text.append(('  %-35s %s'
                                 ) % ('%s%s<%s>' % (var['var'], sep,
                                                    var['type'].replace('=',
                                                                    '> = <')),
                                      var['desc']))
                text.append('')
            return '\n'.join(text)

        def commands_as_text(self):
            text = [_('Commands:')]
            last_rank = None
            cmds = self.result['commands']
            width = self.result.get('width', 8)
            ckeys = cmds.keys()
            ckeys.sort(key=lambda k: cmds[k][3])
            for c in ckeys:
                cmd, args, explanation, rank = cmds[c]
                if not rank or not cmd:
                    continue
                if last_rank and int(rank / 10) != last_rank:
                    text.append('')
                last_rank = int(rank / 10)
                if c[0] == '_':
                    c = '  '
                else:
                    c = '%s|' % c[0]
                fmt = '  %%s%%-%d.%ds' % (width, width)
                if explanation:
                    if len(args or '') <= 15:
                        fmt += ' %-15.15s %s'
                    else:
                        fmt += ' %%s\n%s %%s' % (' ' * (len(c) + width + 18))
                else:
                    fmt += ' %s %s '
                text.append(fmt % (c, cmd.replace('=', ''),
                                   args and ('%s' % (args, )) or '',
                                   (explanation.splitlines() or [''])[0]))
            if 'tags' in self.result:
                text.extend([
                    '',
                _('Tags:  (use a tag as a command to display tagged messages)'),
                    '',
                    self.result['tags'].as_text()
                ])
            return '\n'.join(text)

        def as_text(self):
            if not self.result:
                return _('Error')
            return ''.join([
                ('splash' in self.result) and self.splash_as_text() or '',
              ('variables' in self.result) and self.variables_as_text() or '',
                ('commands' in self.result) and self.commands_as_text() or '',
            ])

    def command(self):
        self.session.ui.reset_marks(quiet=True)
        if self.args:
            command = self.args.pop(0)
            for cls in COMMANDS:
                name = cls.SYNOPSIS[1] or cls.SYNOPSIS[2]
                width = len(name)
                if name and name == command:
                    order = 1
                    cmd_list = {'_main': (name, cls.SYNOPSIS[3],
                                          cls.__doc__, order)}
                    subs = [c for c in COMMANDS
                                    if (c.SYNOPSIS[1] or c.SYNOPSIS[2]
                                        ).startswith(name + '/')]
                    for scls in sorted(subs):
                        sc, scmd, surl, ssynopsis = scls.SYNOPSIS[:4]
                        order += 1
                        cmd_list['_%s' % scmd] = (scmd, ssynopsis,
                                                  scls.__doc__, order)
                        width = max(len(scmd or surl), width)
                    return {
                        'pre': cls.__doc__,
                        'commands': cmd_list,
                        'width': width,
                        'post': cls.EXAMPLES
                    }
            return self._error(_('Unknown command'))

        else:
            cmd_list = {}
            count = 0
            for grp in COMMAND_GROUPS:
                count += 10
                for cls in COMMANDS:
                    c, name, url, synopsis = cls.SYNOPSIS[:4]
                    if cls.ORDER[0] == grp and '/' not in (name or ''):
                        cmd_list[c or '_%s' % name] = (name, synopsis,
                                                       cls.__doc__,
                                                       count + cls.ORDER[1])
            return {
                'commands': cmd_list,
                'tags': GetCommand('tag/list')(self.session).run(),
                'index': self._idx()
            }

    def _starting(self):
        pass

    def _finishing(self, command, rv):
        return self.CommandResult(self.session, self.name, self.SYNOPSIS[2],
                                  command.__doc__ or self.__doc__, rv,
                                  self.status, self.message)


class HelpVars(Help):
    """Print help on Mailpile variables"""
    SYNOPSIS = (None, 'help/variables', 'help/variables', None)
    ORDER = ('Config', 9)

    def command(self):
        config = self.session.config
        result = []
        for cat in config.CATEGORIES.keys():
            variables = []
            for what in config.INTS, config.STRINGS, config.DICTS:
                for ii, i in what.iteritems():
                    variables.append({
                        'var': ii,
                        'type': i[0],
                        'desc': i[2]
                    })
            variables.sort(key=lambda k: k['var'])
            result.append({
                'category': cat,
                'name': config.CATEGORIES[cat][1],
                'variables': variables
            })
        result.sort(key=lambda k: config.CATEGORIES[k['category']][0])
        return {'variables': result}


class HelpSplash(Help):
    """Print Mailpile splash screen"""
    SYNOPSIS = (None, 'help/splash', 'help/splash', None)
    ORDER = ('Config', 9)

    def command(self):
        http_worker = self.session.config.http_worker
        if http_worker:
            http_url = 'http://%s:%s/' % http_worker.httpd.sspec
        else:
            http_url = ''
        return {
            'splash': self.ABOUT,
            'http_url': http_url,
        }


def GetCommand(name):
    match = [c for c in COMMANDS if name in c.SYNOPSIS[:3]]
    if len(match) == 1:
        return match[0]
    return None


def Action(session, opt, arg, data=None):
    session.ui.reset_marks(quiet=True)
    config = session.config

    if not opt:
        return Help(session, 'help').run()

    # Use the COMMANDS dict by default.
    command = GetCommand(opt)
    if command:
        return command(session, opt, arg, data=data).run()

    # Tags are commands
    tag = config.get_tag(opt)
    if tag:
        return GetCommand('search')(session, opt, arg=arg, data=data
                                    ).run(search=['tag:%s' % tag._key])

    # OK, give up!
    raise UsageError(_('Unknown command: %s') % opt)


# Commands starting with _ don't get single-letter shortcodes...
COMMANDS = [
    Optimize, Rescan, RunWWW, UpdateStats, RenderPage,
    ConfigPrint, ConfigSet, ConfigAdd, ConfigUnset, AddMailboxes,
    Output, Help, HelpVars, HelpSplash
]
COMMAND_GROUPS = ['Internals', 'Config', 'Searching', 'Tagging', 'Composing']
