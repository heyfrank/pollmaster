import argparse
import asyncio
import copy
import datetime
import json
import logging
import shlex
import traceback

import discord
import pytz

from discord.ext import commands

from utils.misc import CustomFormatter
from .poll import Poll
from utils.paginator import embed_list_paginated
from essentials.multi_server import get_server_pre, ask_for_server, ask_for_channel
from essentials.settings import SETTINGS
from utils.poll_name_generator import generate_word
from essentials.exceptions import StopWizard


class PollControls:
    def __init__(self, bot):
        self.bot = bot
        self.bot.loop.create_task(self.refresh_polls())
        self.ignore_next_removed_reaction = {}



    # General Methods
    async def refresh_polls(self):
        """This function runs every 5 seconds to refresh poll messages when needed"""
        while True:
            try:
                for i in range(self.bot.poll_refresh_q.qsize()):
                    values = await self.bot.poll_refresh_q.get()
                    if values.get('lock') and not values.get('lock')._waiters:
                        p = await Poll.load_from_db(self.bot, str(values.get('sid')), values.get('label'))
                        if p:
                            await self.bot.edit_message(values.get('msg'), embed=await p.generate_embed())

                        self.bot.poll_refresh_q.task_done()
                    else:
                        await self.bot.poll_refresh_q.put_unique_id(values)
            except AttributeError:
                pass
            await asyncio.sleep(5)

    def get_lock(self, server_id):
        if not self.bot.locks.get(str(server_id)):
            self.bot.locks[server_id] = asyncio.Lock()
        return self.bot.locks.get(str(server_id))

    async def is_admin_or_creator(self, ctx, server, owner_id, error_msg=None):
        member = server.get_member(ctx.message.author.id)
        if member.id == owner_id:
            return True
        elif member.server_permissions.manage_server:
            return True
        else:
            result = await self.bot.db.config.find_one({'_id': str(server.id)})
            if result and result.get('admin_role') in [r.name for r in member.roles]:
                return True
            else:
                if error_msg is not None:
                    await self.bot.send_message(ctx.message.author, error_msg)
                return False

    async def say_error(self, ctx, error_text, footer_text=None):
        embed = discord.Embed(title='', description=error_text, colour=SETTINGS.color)
        embed.set_author(name='Error', icon_url=SETTINGS.author_icon)
        if footer_text is not None:
            embed.set_footer(text=footer_text)
        await self.bot.say(embed=embed)

    async def say_embed(self, ctx, say_text='', title='Pollmaster', footer_text=None):
        embed = discord.Embed(title='', description=say_text, colour=SETTINGS.color)
        embed.set_author(name=title, icon_url=SETTINGS.author_icon)
        if footer_text is not None:
            embed.set_footer(text=footer_text)
        await self.bot.say(embed=embed)

    # Commands
    @commands.command(pass_context=True)
    async def activate(self, ctx, *, short=None):
        """Activate a prepared poll. Parameter: <label>"""
        server = await ask_for_server(self.bot, ctx.message, short)
        if not server:
            return

        if short is None:
            pre = await get_server_pre(self.bot, ctx.message.server)
            error = f'Please specify the label of a poll after the close command. \n' \
                    f'`{pre}activate <poll_label>`'
            await self.say_error(ctx, error)
        else:
            p = await Poll.load_from_db(self.bot, str(server.id), short)
            if p is not None:
                # check if already active, then just do nothing
                if await p.is_active():
                    return
                # Permission Check: Admin or Creator
                if not await self.is_admin_or_creator(
                        ctx, server,
                        p.author.id,
                        'You don\'t have sufficient rights to activate this poll. Please talk to the server admin.'
                ):
                    return

                # Activate Poll
                p.active = True
                await p.save_to_db()
                await ctx.invoke(self.show, short)
            else:
                error = f'Poll with label "{short}" was not found.'
                # pre = await get_server_pre(self.bot, ctx.message.server)
                # footer = f'Type {pre}show to display all polls'
                await self.say_error(ctx, error)
                await ctx.invoke(self.show)

    @commands.command(pass_context=True)
    async def delete(self, ctx, *, short=None):
        '''Delete a poll. Parameter: <label>'''
        server = await ask_for_server(self.bot, ctx.message, short)
        if not server:
            return
        if short is None:
            pre = await get_server_pre(self.bot, ctx.message.server)
            error = f'Please specify the label of a poll after the delete command. \n' \
                    f'`{pre}close <poll_label>`'
            await self.say_error(ctx, error)
        else:
            p = await Poll.load_from_db(self.bot, str(server.id), short)
            if p is not None:
                # Permission Check: Admin or Creator
                if not await self.is_admin_or_creator(
                        ctx, server,
                        p.author.id,
                        'You don\'t have sufficient rights to delete this poll. Please talk to the server admin.'
                ):
                    return False

                # Delete Poll
                result = await self.bot.db.polls.delete_one({'server_id': server.id, 'short': short})
                if result.deleted_count == 1:
                    say = f'Poll with label "{short}" was successfully deleted. This action can\'t be undone!'
                    title = 'Poll deleted'
                    await self.say_embed(ctx, say, title)
                else:
                    error = f'Action failed. Poll could not be deleted. You should probably report his error to the dev, thanks!`'
                    await self.say_error(ctx, error)

            else:
                error = f'Poll with label "{short}" was not found.'
                pre = await get_server_pre(self.bot, ctx.message.server)
                footer = f'Type {pre}show to display all polls'
                await self.say_error(ctx, error, footer)

    @commands.command(pass_context=True)
    async def close(self, ctx, *, short=None):
        '''Close a poll. Parameter: <label>'''
        server = await ask_for_server(self.bot, ctx.message, short)
        if not server:
            return

        if short is None:
            pre = await get_server_pre(self.bot, ctx.message.server)
            error = f'Please specify the label of a poll after the close command. \n' \
                    f'`{pre}close <poll_label>`'
            await self.say_error(ctx, error)
        else:
            p = await Poll.load_from_db(self.bot, str(server.id), short)
            if p is not None:
                # Permission Check: Admin or Creator
                if not await self.is_admin_or_creator(
                        ctx, server,
                        p.author.id,
                        'You don\'t have sufficient rights to close this poll. Please talk to the server admin.'
                ):
                    return False

                # Close Poll
                p.open = False
                await p.save_to_db()
                await ctx.invoke(self.show, short)
            else:
                error = f'Poll with label "{short}" was not found.'
                # pre = await get_server_pre(self.bot, ctx.message.server)
                # footer = f'Type {pre}show to display all polls'
                await self.say_error(ctx, error)
                await ctx.invoke(self.show)

    @commands.command(pass_context=True)
    async def export(self, ctx, *, short=None):
        '''Export a poll. Parameter: <label>'''
        server = await ask_for_server(self.bot, ctx.message, short)
        if not server:
            return

        if short is None:
            pre = await get_server_pre(self.bot, ctx.message.server)
            error = f'Please specify the label of a poll after the close command. \n' \
                    f'`{pre}close <poll_label>`'
            await self.say_error(ctx, error)
        else:
            p = await Poll.load_from_db(self.bot, str(server.id), short)
            if p is not None:
                if p.open:
                    pre = await get_server_pre(self.bot, ctx.message.server)
                    error_text = f'You can only export closed polls. \nPlease `{pre}close {short}` the poll first or wait for the deadline.'
                    await self.say_error(ctx, error_text)
                else:
                    # sending file
                    file = await p.export()
                    if file is not None:
                        await self.bot.send_file(
                            ctx.message.author,
                            file,
                            content='Sending you the requested export of "{}".'.format(p.short)
                        )
                    else:
                        error_text = 'Could not export the requested poll. \nPlease report this to the developer.'
                        await self.say_error(ctx, error_text)
            else:
                error = f'Poll with label "{short}" was not found.'
                # pre = await get_server_pre(self.bot, ctx.message.server)
                # footer = f'Type {pre}show to display all polls'
                await self.say_error(ctx, error)
                await ctx.invoke(self.show)

    @commands.command(pass_context=True)
    async def show(self, ctx, short='open', start=0):
        '''Show a list of open polls or show a specific poll. Parameters: "open" (default), "closed", "prepared" or <label>'''

        server = await ask_for_server(self.bot, ctx.message, short)
        if not server:
            return

        if short in ['open', 'closed', 'prepared']:
            query = None
            if short == 'open':
                query = self.bot.db.polls.find({'server_id': str(server.id), 'open': True, 'active': True})
            elif short == 'closed':
                query = self.bot.db.polls.find({'server_id': str(server.id), 'open': False, 'active': True})
            elif short == 'prepared':
                query = self.bot.db.polls.find({'server_id': str(server.id), 'active': False})

            if query is not None:
                # sort by newest first
                polls = [poll async for poll in query.sort('_id', -1)]
            else:
                return

            def item_fct(i,item):
                return f':black_small_square: **{item["short"]}**: {item["name"]}'

            title = f' Listing {short} polls'
            embed = discord.Embed(title='', description='', colour=SETTINGS.color)
            embed.set_author(name=title, icon_url=SETTINGS.author_icon)
            # await self.bot.say(embed=await self.embed_list_paginated(polls, item_fct, embed))
            # msg = await self.embed_list_paginated(ctx, polls, item_fct, embed, per_page=8)
            pre = await get_server_pre(self.bot, server)
            footer_text = f'type {pre}show <label> to display a poll. '
            msg = await embed_list_paginated(self.bot, pre, polls, item_fct, embed, footer_prefix=footer_text,
                                             per_page=10)
        else:
            p = await Poll.load_from_db(self.bot, str(server.id), short)
            if p is not None:
                error_msg = 'This poll is inactive and you have no rights to display or view it.'
                if not await p.is_active() and not await self.is_admin_or_creator(ctx, server, p.author, error_msg):
                    return
                await p.post_embed()
            else:
                error = f'Poll with label {short} was not found.'
                pre = await get_server_pre(self.bot, server)
                footer = f'Type {pre}show to display all polls'
                await self.say_error(ctx, error, footer)

    @commands.command(pass_context=True)
    async def cmd(self, ctx, *, cmd=None):
        '''The old, command style way paired with the wizard.'''
        await self.say_embed(ctx, say_text='This command is temporarily disabled.')
        # server = await ask_for_server(self.bot, ctx.message)
        # if not server:
        #     return
        # pre = await get_server_pre(self.bot, server)
        #
        # # generate the argparser and handle invalid stuff
        # descr = 'Accept poll settings via commandstring. \n\n' \
        #         '**Wrap all arguments in quotes like this:** \n' \
        #         f'{pre}cmd -question \"What tea do you like?\" -o \"green, black, chai\"\n\n' \
        #         'The Order of arguments doesn\'t matter. If an argument is missing, it will use the default value. ' \
        #         'If an argument is invalid, the wizard will step in. ' \
        #         'If the command string is invalid, you will get this error :)'
        # parser = argparse.ArgumentParser(description=descr, formatter_class=CustomFormatter, add_help=False)
        # parser.add_argument('-question', '-q')
        # parser.add_argument('-label', '-l', default=str(await generate_word(self.bot, server.id)))
        # parser.add_argument('-options', '-o')
        # parser.add_argument('-multiple_choice', '-mc', default='1')
        # parser.add_argument('-roles', '-r', default='all')
        # parser.add_argument('-weights', '-w', default='none')
        # parser.add_argument('-deadline', '-d', default='0')
        # parser.add_argument('-anonymous', '-a', action="store_true")
        #
        # helpstring = parser.format_help()
        # helpstring = helpstring.replace("pollmaster.py", f"{pre}cmd ")
        #
        # if cmd and cmd == 'help':
        #     await self.say_embed(ctx, say_text=helpstring)
        #     return
        #
        # try:
        #     cmds = shlex.split(cmd)
        # except ValueError:
        #     await self.say_error(ctx, error_text=helpstring)
        #     return
        # except:
        #     return
        #
        # try:
        #     args, unknown_args = parser.parse_known_args(cmds)
        # except SystemExit:
        #     await self.say_error(ctx, error_text=helpstring)
        #     return
        # except:
        #     return
        #
        # if unknown_args:
        #     error_text = f'**There was an error reading the command line options!**.\n' \
        #                  f'Most likely this is because you didn\'t surround the arguments with double quotes like this: ' \
        #                  f'`{pre}cmd -q "question of the poll" -o "yes, no, maybe"`' \
        #                  f'\n\nHere are the arguments I could not understand:\n'
        #     error_text += '`'+'\n'.join(unknown_args)+'`'
        #     error_text += f'\n\nHere are the arguments which are ok:\n'
        #     error_text += '`' + '\n'.join([f'{k}: {v}' for k, v in vars(args).items()]) + '`'
        #
        #     await self.say_error(ctx, error_text=error_text, footer_text=f'type `{pre}cmd help` for details.')
        #     return
        #
        # # pass arguments to the wizard
        # async def route(poll):
        #     await poll.set_name(force=args.question)
        #     await poll.set_short(force=args.label)
        #     await poll.set_anonymous(force=f'{"yes" if args.anonymous else "no"}')
        #     await poll.set_options_reaction(force=args.options)
        #     await poll.set_multiple_choice(force=args.multiple_choice)
        #     await poll.set_roles(force=args.roles)
        #     await poll.set_weights(force=args.weights)
        #     await poll.set_duration(force=args.deadline)
        #
        # poll = await self.wizard(ctx, route, server)
        # if poll:
        #     await poll.post_embed(destination=poll.channel)


    @commands.command(pass_context=True)
    async def quick(self, ctx, *, cmd=None):
        '''Create a quick poll with just a question and some options. Parameters: <Question> (optional)'''
        server = await ask_for_server(self.bot, ctx.message)
        if not server:
            return

        async def route(poll):
            await poll.set_name(force=cmd)
            await poll.set_short(force=str(await generate_word(self.bot, server.id)))
            await poll.set_anonymous(force='no')
            await poll.set_options_reaction()
            await poll.set_multiple_choice(force='1')
            await poll.set_roles(force='all')
            await poll.set_weights(force='none')
            await poll.set_duration(force='0')

        poll = await self.wizard(ctx, route, server)
        if poll:
            await poll.post_embed(destination=poll.channel)

    @commands.command(pass_context=True)
    async def prepare(self, ctx, *, cmd=None):
        '''Prepare a poll to use later. Parameters: <Question> (optional) '''
        server = await ask_for_server(self.bot, ctx.message)
        if not server:
            return

        async def route(poll):
            await poll.set_name(force=cmd)
            await poll.set_short()
            await poll.set_preparation()
            await poll.set_anonymous()
            await poll.set_options_reaction()
            await poll.set_multiple_choice()
            await poll.set_roles()
            await poll.set_weights()
            await poll.set_duration()

        poll = await self.wizard(ctx, route, server)
        if poll:
            await poll.post_embed(destination=ctx.message.author)

    @commands.command(pass_context=True)
    async def new(self, ctx, *, cmd=None):
        '''Start the poll wizard to create a new poll step by step. Parameters: <Question> (optional) '''
        server = await ask_for_server(self.bot, ctx.message)
        if not server:
            return

        async def route(poll):
            await poll.set_name(force=cmd)
            await poll.set_short()
            await poll.set_anonymous()
            await poll.set_options_reaction()
            await poll.set_multiple_choice()
            await poll.set_roles()
            await poll.set_weights()
            await poll.set_duration()

        poll = await self.wizard(ctx, route, server)
        if poll:
            await poll.post_embed(destination=poll.channel)

    # The Wizard!
    async def wizard(self, ctx, route, server):
        channel = await ask_for_channel(self.bot, server, ctx.message)
        if not channel:
            return

        # Permission Check
        member = server.get_member(ctx.message.author.id)
        if not member.server_permissions.manage_server:
            result = await self.bot.db.config.find_one({'_id': str(server.id)})
            if result and result.get('admin_role') not in [r.name for r in member.roles] and result.get(
                    'user_role') not in [r.name for r in member.roles]:
                pre = await get_server_pre(self.bot, server)
                await self.bot.send_message(ctx.message.author,
                                            'You don\'t have sufficient rights to start new polls on this server. '
                                            'A server administrator has to assign the user or admin role to you. '
                                            f'To view and set the permissions, an admin can use `{pre}userrole` and '
                                            f'`{pre}adminrole`')
                return

        ## Create object
        poll = Poll(self.bot, ctx, server, channel)

        ## Route to define object, passed as argument for different constructors
        try:
            await route(poll)
            poll.finalize()
        except StopWizard:
            return

        # Finalize
        await poll.save_to_db()
        return poll

    # BOT EVENTS (@bot.event)
    async def on_socket_raw_receive(self, raw_msg):
        if not isinstance(raw_msg, str):
            return
        msg = json.loads(raw_msg)
        type = msg.get("t")
        data = msg.get("d")
        if not data:
            return
        emoji = data.get("emoji")
        user_id = data.get("user_id")
        message_id = data.get("message_id")
        if type == "MESSAGE_REACTION_ADD":
            await self.do_on_reaction_add(data)
        elif type == "MESSAGE_REACTION_REMOVE":
            await self.do_on_reaction_remove(data)

    async def do_on_reaction_remove(self, data):
        # get emoji symbol
        emoji = data.get('emoji')
        if emoji:
            emoji = emoji.get('name')
        if not emoji:
            return

        # check if removed by the bot.. this is a bit hacky but discord doesn't provide the correct info...
        message_id = data.get('message_id')
        user_id = data.get('user_id')
        if self.ignore_next_removed_reaction.get(str(message_id)+str(emoji)) == user_id:
            del self.ignore_next_removed_reaction[str(message_id)+str(emoji)]
            return


        # check if we can find a poll label
        channel_id = data.get('channel_id')

        channel = self.bot.get_channel(channel_id)
        user = await self.bot.get_user_info(user_id)  # only do this once
        if not channel:
            # discord rapidly closes dm channels by desing
            # put private channels back into the bots cache and try again
            await self.bot.start_private_message(user)
            channel = self.bot.get_channel(channel_id)
        message = await self.bot.get_message(channel=channel, id=message_id)
        label = None
        if message and message.embeds:
            embed = message.embeds[0]
            label_object = embed.get('author')
            if label_object:
                label_full = label_object.get('name')
                if label_full and label_full.startswith('>> '):
                    label = label_full[3:]
        if not label:
            return

        # fetch poll
        # create message object for the reaction sender, to get correct server
        user_msg = copy.deepcopy(message)
        user_msg.author = user
        server = await ask_for_server(self.bot, user_msg, label)

        # this is exclusive

        lock = self.get_lock(server.id)
        async with lock:
            # try to load poll form cache
            p = self.bot.poll_cache.get(str(server.id) + label)
            if not p:
                p = await Poll.load_from_db(self.bot, server.id, label)
            if not isinstance(p, Poll):
                return

            if not p.anonymous:
                # for anonymous polls we can't unvote because we need to hide reactions
                member = server.get_member(user_id)
                await p.unvote(member, emoji, message, lock)


    async def do_on_reaction_add(self, data):
        # dont look at bot's own reactions
        user_id = data.get('user_id')
        if user_id == self.bot.user.id:
            return

        # get emoji symbol
        emoji = data.get('emoji')
        if emoji:
            emoji = emoji.get('name')
        if not emoji:
            return

        # check if we can find a poll label
        message_id = data.get('message_id')
        channel_id = data.get('channel_id')
        channel = self.bot.get_channel(channel_id)
        user = await self.bot.get_user_info(user_id)  # only do this once
        if not channel:
            # discord rapidly closes dm channels by desing
            # put private channels back into the bots cache and try again
            await self.bot.start_private_message(user)
            channel = self.bot.get_channel(channel_id)
        message = await self.bot.get_message(channel=channel, id=message_id)
        label = None
        if message and message.embeds:
            embed = message.embeds[0]
            label_object = embed.get('author')
            if label_object:
                label_full = label_object.get('name')
                if label_full and label_full.startswith('>> '):
                    label = label_full[3:]
        if not label:
            return

        # fetch poll
        # create message object for the reaction sender, to get correct server
        user_msg = copy.deepcopy(message)
        user_msg.author = user
        server = await ask_for_server(self.bot, user_msg, label)

        # this is exclusive to keep database access sequential
        # hopefully it will scale well enough or I need a different solution
        lock = self.get_lock(server.id)
        async with lock:
            p = self.bot.poll_cache.get(str(server.id)+label)
            if not p:
                p = await Poll.load_from_db(self.bot, server.id, label)
            if not isinstance(p, Poll):
                return

            # export
            if emoji == '📎':
                # sending file
                file = await p.export()
                if file is not None:
                    await self.bot.send_file(
                        user,
                        file,
                        content='Sending you the requested export of "{}".'.format(p.short)
                    )
                return

            # info
            member = server.get_member(user_id)
            if emoji == '❔':
                is_open = await p.is_open()
                embed = discord.Embed(title=f"Info for the {'CLOSED ' if not is_open else ''}poll \"{p.name}\"",
                                      description='', color=SETTINGS.color)
                embed.set_author(name=f" >> {p.short}", icon_url=SETTINGS.author_icon)

                # vote rights
                vote_rights = await p.has_required_role(member)
                embed.add_field(name=f'{"Can you vote?" if is_open else "Could you vote?"}',
                                value=f'{"✅" if vote_rights else "❎"}', inline=False)

                # edit rights
                edit_rights = False
                if str(member.id) == str(p.author):
                    edit_rights = True
                elif member.server_permissions.manage_server:
                    edit_rights = True
                else:
                    result = await self.bot.db.config.find_one({'_id': str(server.id)})
                    if result and result.get('admin_role') in [r.name for r in member.roles]:
                        edit_rights = True
                embed.add_field(name='Can you manage the poll?', value=f'{"✅" if edit_rights else "❎"}', inline=False)

                # choices
                choices = 'You have not voted yet.' if vote_rights else 'You can\'t vote in this poll.'
                if user.id in p.votes:
                    if p.votes[user.id]['choices'].__len__() > 0:
                        choices = ', '.join([p.options_reaction[c] for c in p.votes[user.id]['choices']])
                embed.add_field(name=f'{"Your current votes (can be changed as long as the poll is open):" if is_open else "Your final votes:"}',
                                value=choices, inline=False)

                # weight
                if vote_rights:
                    weight = 1
                    if p.weights_roles.__len__() > 0:
                        valid_weights = [p.weights_numbers[p.weights_roles.index(r)] for r in
                                         list(set([n.name for n in member.roles]).intersection(set(p.weights_roles)))]
                        if valid_weights.__len__() > 0:
                            weight = max(valid_weights)
                else:
                    weight = 'You can\'t vote in this poll.'
                embed.add_field(name='Weight of your votes:', value=weight, inline=False)

                # time left
                deadline = p.get_duration_with_tz()
                if not is_open:
                    time_left = 'This poll is closed.'
                elif deadline == 0:
                    time_left = 'Until manually closed.'
                else:
                    time_left = str(deadline-datetime.datetime.utcnow().replace(tzinfo=pytz.utc)).split('.', 2)[0]

                embed.add_field(name='Time left in the poll:', value=time_left, inline=False)

                await self.bot.send_message(user, embed=embed)
                return

            # Assume: User wants to vote with reaction
            # no rights, terminate function
            if not await p.has_required_role(member):
                await self.bot.remove_reaction(message, emoji, user)
                await self.bot.send_message(user, f'You are not allowed to vote in this poll. Only users with '
                                                  f'at least one of these roles can vote:\n{", ".join(p.roles)}')
                return

            # check if we need to remove reactions (this will trigger on_reaction_remove)
            if str(channel.type) != 'private' and p.anonymous:
                # immediately remove reaction and to be safe, remove all reactions
                self.ignore_next_removed_reaction[str(message.id)+str(emoji)] = user_id
                await self.bot.remove_reaction(message, emoji, user)

            # order here is crucial since we can't determine if a reaction was removed by the bot or user
            # update database with vote
            await p.vote(member, emoji, message, lock)

            # cant do this until we figure out how to see who removed the reaction?
            # for now MC 1 is like MC x
            # if str(channel.type) != 'private' and p.multiple_choice == 1:
            #     # remove all other reactions
            #     # if lock._waiters.__len__() == 0:
            #     for r in message.reactions:
            #         if r.emoji and r.emoji != emoji:
            #             await self.bot.remove_reaction(message, r.emoji, user)
            #     pass


def setup(bot):
    global logger
    logger = logging.getLogger('bot')
    bot.add_cog(PollControls(bot))
