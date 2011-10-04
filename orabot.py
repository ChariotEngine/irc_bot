# Copyright 2011 orabot Developers
#
# This file is part of orabot, which is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import socket, multiprocessing, time
import os
import re
from datetime import date
import sqlite3
import urllib.request
import imp
import inspect
import signal

import db_process
import notifications
import config
import spam_filter
from commands import *

###
if not os.path.exists('db/openra.sqlite'):
    db_process.start()
###

# Defining a class to run the server. One per connection. This class will do most of our work.
class IRC_Server:

    # The default constructor - declaring our global variables
    # This needs to support an alternate nick.
    def __init__(self, host, port, nick, channel , password =""):
        self.irc_host = host
        self.irc_port = port
        self.irc_nick = nick
        self.irc_channel = channel
        self.irc_sock = socket.socket ( socket.AF_INET, socket.SOCK_STREAM )
        self.is_connected = False
        self.should_reconnect = False
        self.command = ""

    ## The destructor - Close socket.
    def __del__(self):
        self.irc_sock.close()

    # This is the bit that controls connection to a server & channel.
    def connect(self):
        self.should_reconnect = True
        try:
            self.irc_sock.connect ((self.irc_host, self.irc_port))
        except:
            print ("Error: Could not connect to IRC; Host: " + str(self.irc_host) + "Port: " + str(self.irc_port))
            exit(1) # We should make it recconect if it gets an error here
        print ("Connected to: " + str(self.irc_host) + ":" + str(self.irc_port))

        str_buff = ("NICK %s \r\n") % (self.irc_nick)
        self.irc_sock.send (str_buff.encode())
        print ("Setting bot nick to " + str(self.irc_nick) )
        time.sleep(2)
        recv = self.irc_sock.recv( 4096 )
        recv=self.decode_stream(recv)
        if str(recv).find ( " 433 * "+self.irc_nick+" " ) != -1:
            print('Nick is already in use!!! Change nickname and restart bot!')
            return

        str_buff = ("USER %s 8 * :X\r\n") % (self.irc_nick)
        self.irc_sock.send (str_buff.encode())
        print ("Setting User")
        # Insert Alternate nick code here.

        if config.nickserv == True:
            print ("Attempting to identify with NickServ...")
            data = "identify "+config.nickserv_password
            time.sleep(3)
            self.irc_sock.send( (("PRIVMSG %s :%s\r\n") % ('NickServ', data)).encode() )
            time.sleep(3)
            recv = self.irc_sock.recv( 8192 )
            recv=self.decode_stream(recv)

            if str(recv).find ( " NOTICE "+config.bot_nick+" :You are now identified for " ) != -1:
                print("Identification succeeded")
            else:
                print("### Identification failed! ###")

        for i in range(len(self.irc_channel)):
            str_buff = ( "JOIN %s \r\n" ) % (self.irc_channel[i])
            self.irc_sock.send (str_buff.encode())
            print ("Joining channel " + self.irc_channel[i] )

        ### change existing users status to offline if their status in DB is online but they are not on any of the channels and upside down
        conn = sqlite3.connect('../db/openra.sqlite')
        cur = conn.cursor()
        sql = """SELECT user,state FROM users
        """
        cur.execute(sql)
        records = cur.fetchall()
        conn.commit()
        time.sleep(3)
        if ( len(records) != 0 ):
            user_nicks = self.parse_names(self.get_names(config.channels.split(',')[0]))
            for chan in config.channels.split(','):
                time.sleep(2)
                user_nicks = self.parse_names(self.get_names(chan))
                if ( len(user_nicks) != 0 ):    #no error on NAMES
                    for i in range(len(records)):
                        if ( records[i][0] not in user_nicks ):
                            if ( str(records[i][1]) == '1' ):
                                sql = """UPDATE users
                                        SET state = 0
                                        WHERE user = '"""+records[i][0]+"""'
                                """
                                cur.execute(sql)
                                conn.commit()
                        else:
                            if ( str(records[i][1]) == '0' ):
                                sql = """UPDATE users
                                        SET state = 1
                                        WHERE user = '"""+records[i][0]+"""'
                                """
                                cur.execute(sql)
                                conn.commit()
        cur.close()
        ###

        self.is_connected = True
        self.listen()

    def listen(self):
        while self.is_connected:
            recv = self.irc_sock.recv( 4096 )
            recv=self.decode_stream(recv)

            if str(recv).find ( "PING" ) != -1:
                self.irc_sock.send ( ("PONG "+ recv.split() [ 1 ] + "\r\n").encode() )

            if str(recv).find ( " PRIVMSG " ) != -1:
                irc_user_nick = str(recv).split ( '!' ) [ 0 ] . split ( ":")[1]
                irc_user_host = str(recv).split ( '@' ) [ 1 ] . split ( ' ' ) [ 0 ]
                irc_user_message = self.data_to_message(str(recv))
                chan = (str(recv)).split()[2]  #channel
                ###logs
                if re.search('^.*01ACTION', irc_user_message) and re.search('01$', irc_user_message):
                    irc_user_message_me = irc_user_message.split('01ACTION ')[1][0:-4]
                    self.logs(irc_user_nick, chan, 'action', str(irc_user_message_me), '')
                else:
                    self.logs(irc_user_nick, chan, 'privmsg', str(irc_user_message), '')
                ### logs end

                print ( irc_user_nick + ": " + irc_user_message)
                # Message starts with command prefix?
                if ( str(irc_user_message) != '' ):
                    if ( str(irc_user_message[0]) == config.command_prefix ):
                        self.command = str(irc_user_message[1:])
                        self.process_command(irc_user_nick, ( chan ))
                ### parse links and bug reports numbers
                self.parse_link(chan, str(irc_user_message))
                self.parse_bug_num(chan, str(irc_user_message))
                ###

            if str(recv).find ( " JOIN " ) != -1:
                conn = sqlite3.connect('../db/openra.sqlite')   # connect to database
                cur=conn.cursor()
                irc_join_nick = str(recv).split( '!' ) [ 0 ].split( ':' ) [ 1 ]
                if ( len(irc_join_nick.split()) == 1 ):
                    supy_host = str(recv).split()[0].split('!')[1]
                    chan = str(recv).split()[2].strip()
                    ###logs
                    self.logs(irc_join_nick, chan, 'join', str(supy_host), '')
                    ###

                    ### for pingme
                    sql = """SELECT who,users_back FROM pingme
                    """
                    cur.execute(sql)
                    records = cur.fetchall()
                    conn.commit()
                    if ( len(records) != 0 ):
                        for i in range(len(records)):
                            who = records[i][0]
                            users_back = records[i][1].split(',')
                            if ( irc_join_nick in users_back ):
                                self.send_reply( (irc_join_nick +' has joined IRC!'), who, who )
                                records_index = users_back.index(irc_join_nick)
                                del users_back[records_index]
                                users_back = ",".join(users_back)
                                if ( len(users_back) == 0 ):
                                    sql = """DELETE FROM pingme
                                            WHERE who = '"""+who+"""'
                                    """
                                    cur.execute(sql)
                                    conn.commit()
                                else:
                                    sql = """UPDATE pingme
                                            SET users_back = '"""+users_back+"""'
                                            WHERE who = '"""+who+"""'
                                    """
                                    cur.execute(sql)
                                    conn.commit()
                    ###
                    sql = """SELECT * FROM users
                            WHERE user = '"""+irc_join_nick+"'"+"""
                    """
                    cur.execute(sql)
                    conn.commit()
                    row = []
                    for row in cur:
                        pass
                    if irc_join_nick not in row:     #user NOT found, add him (if user is not in db, he could not have ]later message)
                        sql = """INSERT INTO users
                                (user,state,channels)
                                VALUES
                                (
                                '"""+str(irc_join_nick)+"""',1,'"""+chan+"""'
                                )
                        """
                        cur.execute(sql)
                        conn.commit()
                    else:   #user is in `users` table; he can have ]later messages
                        #for ]last and for logs (add channel in list)
                        sql = """SELECT channels FROM users
                                WHERE user = '"""+irc_join_nick+"""'
                        """
                        cur.execute(sql)
                        records = cur.fetchall()
                        conn.commit()
                        if ( records[0][0] == '' ) or ( str(records[0][0]) == 'None' ):
                            channel_to_db = chan
                        else:
                            channel_to_db = records[0][0]+','+chan
                        sql = """UPDATE users
                                SET state = 1, channels = '"""+channel_to_db+"""'
                                WHERE user = '"""+str(irc_join_nick)+"""'
                        """
                        cur.execute(sql)
                        conn.commit()
                        sql = """SELECT reciever FROM later
                                WHERE reciever = '"""+irc_join_nick+"'"+"""
                        """
                        cur.execute(sql)
                        conn.commit()

                        row = []
                        for row in cur:
                            pass
                        if irc_join_nick in row:    #he has messages in database, read it
                            sql = """SELECT * FROM later
                                    WHERE reciever = '"""+irc_join_nick+"'"+"""
                            """
                            cur.execute(sql)
                            conn.commit()
                            row = []
                            msgs = []
                            for row in cur:
                                msgs.append(row)
                            msgs_length = len(msgs) #number of messages for player
                            self.send_message_to_channel( ("You have "+str(msgs_length)+" offline messages:"), irc_join_nick )
                            for i in range(int(msgs_length)):
                                who_sent = msgs[i][1]
                                on_channel = msgs[i][3]
                                message_date = msgs[i][4]
                                offline_message = msgs[i][5]
                                self.send_message_to_channel( ("### From: "+who_sent+";  channel: "+on_channel+";  date: "+message_date), irc_join_nick )
                                self.send_message_to_channel( (offline_message), irc_join_nick )
                            time.sleep(0.1)
                            sql = """DELETE FROM later
                                    WHERE reciever = '"""+irc_join_nick+"'"+"""

                            """
                            cur.execute(sql)
                            conn.commit()
                cur.close()

            if str(recv).find ( " QUIT " ) != -1:
                conn = sqlite3.connect('../db/openra.sqlite')   # connect to database
                cur=conn.cursor()
                irc_quit_nick = str(recv).split( "!" )[ 0 ].split( ":" ) [ 1 ]
                supy_host = str(recv).split()[0].split('!')[1]
                ### for ]last and logs
                sql = """SELECT channels FROM users
                        WHERE user = '"""+irc_quit_nick+"""'
                """
                cur.execute(sql)
                records = cur.fetchall()
                conn.commit()
                if ( len(records) == 0 ):   #user not found in table users
                    for chan in config.log_channels.split(','):
                        self.logs(irc_quit_nick, chan, 'quit', str(supy_host), '')
                else:   #user found
                    if ( records[0][0] == '' ) or ( str(records[0][0]) == 'None' ):  #no channels found; reason(probably bot was offline when user joined or user was added manually)
                        for chan in config.log_channels.split(','):
                            self.logs(irc_quit_nick, chan, 'quit', str(supy_host), '')
                    else:   #there are channels
                        db_channels = records[0][0].split(',')
                        for chan in db_channels:
                            self.logs(irc_quit_nick, chan, 'quit', str(supy_host), '')
                sql = """UPDATE users
                        SET date = strftime('%Y-%m-%d-%H-%M-%S'), state = 0, channels = ''
                        WHERE user = '"""+str(irc_quit_nick)+"'"+"""
                """
                cur.execute(sql)
                conn.commit()
                ### for ping me
                sql = """DELETE FROM pingme
                        WHERE who = '"""+irc_quit_nick+"""'
                """
                cur.execute(sql)
                conn.commit()
                ### for ]pick
                modes = ['1v1','2v2','3v3','4v4','5v5']
                diff_mode = ''
                for diff_mode in modes:
                    sql = """DELETE FROM pickup_"""+diff_mode+"""
                            WHERE name = '"""+irc_quit_nick+"""'
                    """
                    cur.execute(sql)
                    conn.commit()
                ### for notify
                sql = """DELETE FROM notify
                        WHERE user = '"""+irc_quit_nick+"""' AND timeout <> 'f' AND timeout <> 'forever'
                """
                cur.execute(sql)
                conn.commit()
                cur.close()

            if str(recv).find ( " PART " ) != -1:
                conn = sqlite3.connect('../db/openra.sqlite')   # connect to database
                cur=conn.cursor()
                irc_part_nick = str(recv).split( "!" )[ 0 ].split( ":" ) [ 1 ]
                supy_host = str(recv).split()[0].split('!')[1]
                chan = str(recv).split()[2].strip()
                ###logs
                self.logs(irc_part_nick, chan, 'part', str(supy_host), '')
                ###
                ### for ]last  and logs
                sql = """SELECT channels FROM users
                        WHERE user = '"""+irc_part_nick+"""'
                """
                cur.execute(sql)
                records = cur.fetchall()
                conn.commit()
                channel_from_db = ''
                if ( len(records) != 0 ):
                    if not (( records[0][0] == '' ) or ( str(records[0][0]) == 'None' )):
                        db_channels = records[0][0].split(',')
                        if chan in db_channels:
                            chan_index = db_channels.index(chan)
                            del db_channels[chan_index]
                            channel_from_db = ",".join(db_channels)
                        else:
                            channel_from_db = ",".join(db_channels)
                sql = """UPDATE users
                        SET date = strftime('%Y-%m-%d-%H-%M-%S'), state = 0, channels = '"""+channel_from_db+"""'
                        WHERE user = '"""+str(irc_part_nick)+"'"+"""
                """
                cur.execute(sql)
                conn.commit()
                ### for ping me
                sql = """DELETE FROM pingme
                        WHERE who = '"""+irc_part_nick+"""'
                """
                cur.execute(sql)
                conn.commit()
                ### for ]pick
                modes = ['1v1','2v2','3v3','4v4','5v5']
                diff_mode = ''
                for diff_mode in modes:
                    sql = """DELETE FROM pickup_"""+diff_mode+"""
                            WHERE name = '"""+irc_part_nick+"""'
                    """
                    cur.execute(sql)
                    conn.commit()
                ### for notify
                sql = """DELETE FROM notify
                        WHERE user = '"""+irc_part_nick+"""' AND timeout <> 'f' AND timeout <> 'forever'
                """
                cur.execute(sql)
                conn.commit()
                cur.close()

            if str(recv).find ( " NICK " ) != -1:
                original_nick = str(recv).split(':')[1].split('!')[0]
                new_nick = str(recv).split()[2].replace(':','').replace('\r\n','')
                conn = sqlite3.connect('../db/openra.sqlite')
                cur = conn.cursor()
                ### for logs
                sql = """SELECT channels FROM users
                        WHERE user = '"""+original_nick+"""'
                """
                cur.execute(sql)
                records = cur.fetchall()
                conn.commit()
                if ( len(records) == 0 ):   #user not found in table users
                    for chan in config.log_channels.split(','):
                        self.logs(original_nick, chan, 'nick', new_nick, '')
                else:   #user found
                    if ( records[0][0] == '' ) or ( str(records[0][0]) == 'None' ):  #no channels found; reason(probably bot was offline when user joined or user was added manually)
                        for chan in config.log_channels.split(','):
                            self.logs(original_nick, chan, 'nick', new_nick, '')
                    else:   #there are channels
                        db_channels = records[0][0].split(',')
                        for chan in db_channels:
                            self.logs(original_nick, chan, 'nick', new_nick, '')
                ###
                sql = """UPDATE users
                        SET state = 0, date = strftime('%Y-%m-%d-%H-%M-%S')
                        WHERE user = '"""+original_nick+"""'
                """
                cur.execute(sql)
                conn.commit()
                sql = """SELECT user FROM users
                        WHERE user = '"""+new_nick+"""'
                """
                cur.execute(sql)
                records = cur.fetchall()
                conn.commit()
                if ( len(records) == 0 ):
                    sql = """INSERT INTO users
                            (user,state,channels)
                            VALUES
                            (
                            '"""+new_nick+"""',1,'"""+chan+"""'
                            )
                    """
                    cur.execute(sql)
                    conn.commit()
                else:
                    sql = """UPDATE users
                            SET state = 1
                            WHERE user = '"""+new_nick+"""'
                    """
                    cur.execute(sql)
                    conn.commit()
                cur.close()

            if str(recv).find ( " TOPIC " ) != -1:
                nick = str(recv).split(':')[1].split('!')[0]
                topic = " ".join(str(recv).split()[3:]).replace(':','').replace('\r\n','')
                chan = str(recv).split()[2]
                self.logs(nick, chan, 'topic', topic, '')

            if str(recv).find ( " KICK " ) != -1:
                by = str(recv).split(':')[1].split('!')[0]
                whom = str(recv).split()[3]
                chan = str(recv).split()[2]
                reason = " ".join(str(recv).split()[4:]).replace(':','').replace('\r\n','')
                self.logs(whom, chan, 'kick', by, reason)

        if self.should_reconnect:
            self.connect()

    def data_to_message(self,data):
        data=data[data.find(" :")+2:] # Notice the space before the :
        return data[:-2] # Without \r\n

    # helper to remove some insanity.
    def send_reply(self,data,user,channel):
        target = channel if channel.startswith('#') else user
        self.send_message_to_channel(data,target)

    #another helper
    def decode_stream(self,stream):
        try:
            return stream.decode("utf8")
        except:
            return stream.decode("CP1252")

    # This function sends a message to a channel or user
    def send_message_to_channel(self,data,channel):
        print ( ( "%s: %s") % (self.irc_nick, data[:256]) )
        while True:
            try:
                self.irc_sock.send( (("PRIVMSG %s :%s\r\n") % (channel, data[:256])).encode() )
            except socket.error as e:
                print("Socket Error: ", e)
                continue
            break
        ### logs
        self.logs(self.irc_nick, channel, 'privmsg', str(data), '')

    def send_notice(self, data, user):
        print ( ( "NOTICE to %s: %s" ) % (user, data) )
        str_buff = ( "NOTICE %s :%s\r\n" ) % (user,data)
        self.irc_sock.send (str_buff.encode())

    def get_names(self, channel):
        str_buff = ( "NAMES %s \r\n" ) % (channel)
        self.irc_sock.send (str_buff.encode())
        #recover all nicks on channel
        time.sleep(2)
        recv = self.irc_sock.recv( 4096 )
        recv = self.decode_stream( recv )
        return recv

    def parse_names(self, recv):
        user_nicks = []
        if recv.find ( " 353 "+config.bot_nick ) != -1:
            user_nicks = recv.split(':')[2].rstrip()
            user_nicks = user_nicks.replace('+','').replace('@','').replace('%','')
            user_nicks = user_nicks.split(' ')
        return user_nicks

    # This function takes a channel, which must start with a #.
    def join_channel(self,channel):
        if (channel[0] == "#"):
            str_buff = ( "JOIN %s \r\n" ) % (channel)
            self.irc_sock.send (str_buff.encode())
            # This needs to test if the channel is full

    # This function takes a channel, which must start with a #.
    def quit_channel(self,channel):
        if (channel[0] == "#"):
            str_buff = ( "PART %s \r\n" ) % (channel)
            self.irc_sock.send ( str_buff.encode() )
            # This needs to modify the list of active channels
    def topic(self, channel, topic):
        str_buff = ("PRIVMSG ChanServ :TOPIC %s %s\r\n") % (channel, topic)
        self.irc_sock.send ( str_buff.encode() )

    def logs(self, irc_user, channel, logs_of, some_data, some_more_data):
        if config.write_logs == True:
            chan_d = str(channel).replace('#','')
            t = time.localtime( time.time() )
            time_prefix = time.strftime( '%Y-%m-%dT%T', t )
            filename = config.log_dir + chan_d + time.strftime( '/%Y/%m/%d', t )
            if channel in config.log_channels.split(','):
                if ( logs_of == 'privmsg' ):
                    row = ' <'+irc_user+'> '+some_data+'\n'
                elif ( logs_of == 'action' ):
                    row = ' * '+irc_user+' '+some_data+'\n'
                elif ( logs_of == 'join' ):
                    row = ' *** '+irc_user+' <'+some_data+'> has joined '+channel+'\n'
                elif ( logs_of == 'quit' ):
                    row = ' *** '+irc_user+' <'+some_data+'> has quit IRC\n'
                elif ( logs_of == 'part' ):
                    row = ' *** '+irc_user+' <'+some_data+'> has left '+channel+'\n'
                elif ( logs_of == 'nick' ):
                    row = ' *** '+irc_user+' is now known as '+some_data+'\n'
                elif ( logs_of == 'topic' ):
                    row = ' *** '+irc_user+' changes topic to "'+some_data+'"\n'
                elif ( logs_of == 'kick' ):
                    row = ' *** '+irc_user+' was kicked by '+some_data+' ('+some_more_data+')\n'
                else:
                    return  # probably an error.
                dir = os.path.dirname(filename)
                try:
                    if not os.path.exists(dir):
                        os.makedirs(dir)
                    file = open(filename,'a')
                    file.write(time_prefix + row)
                    file.close()
                except:
                    print('####### ERROR !!! ###### Probably no write permissions to logs directory!')

    def title_from_url(self, url):
        # todo: security: can force the bot to output anything we like into
        #                 the channel.
        data = urllib.request.urlopen(url).read(4096)
        try:
            encoding = str(data).lower().split('charset=')[1].split('"')[0]
            data = data.decode(encoding)
        except: #no encoding found
            data = data.decode('utf-8')
        title = data.split('<title>')[1].split('</title>')[0].strip()
        return title

    def parse_link(self, channel, message):
        if re.search('.*http.*://.*', message):
            flood_protection = 0
            matches = re.findall(r"http.?://[^\s]*", message)
            for http_link in matches:
                flood_protection = flood_protection + 1
                if flood_protection == 5:
                    time.sleep(6)
                    flood_protection = 0
                link = http_link.split('://')[1]
                pre = http_link.split('http')[1].split('//')[0]
                link = 'http'+pre+'//'+link
                if re.search("^#", channel):
                    if re.search('http.*youtube.com/watch.*', link):
                        link = link.split('&')[0]
                        try:
                            title = self.title_from_url(link).split('- YouTube')[0].rstrip().replace('&amp;','&').replace('&#39;', '\'')
                            if ( title != 'YouTube - Broadcast Yourself.' ):    #video exists
                                self.send_message_to_channel( ("Youtube: "+str(title)), channel )
                        except:
                            pass    #probably socket error in title_from_url() or remote page has charset bot can not decode
                    else:
                        try:
                            title = self.title_from_url(link).replace('\n','').replace('&amp;','&').replace('&#39;', '\'')
                            self.send_message_to_channel( ("Title: "+title), channel )
                        except:
                            pass    #probably socket error in title_from_url() or remote page has charset bot can not decode
            flood_protection = 0

    def parse_bug_num(self, channel, message):
        if re.search('.*\#[0-9]*.*', message):
            flood_protection = 0
            matches = re.findall(r"#[0-9]*", message)
            if re.search("^#", channel):
                for bug_report in matches:
                    flood_protection = flood_protection + 1
                    if flood_protection == 5:
                        time.sleep(6)
                        flood_protection = 0
                    bug_or_feature_num = bug_report.split('#')[1]
                    url = 'http://bugs.open-ra.org/issues/'+bug_or_feature_num
                    try:
                        fetched = self.title_from_url(url).split('OpenRA - ')[1].split(' - open-ra')[0]
                        self.send_message_to_channel( (fetched+" | "+url), channel )
                    except:
                        pass
                flood_protection = 0

    # This function is for pickup matches code
    def players_for_mode(self, mode):
        return sum( map( int, mode.split('v') ) )

    # Special admin commands for Op/HalfOp/Voice
    def OpVoice(self, user, channel):
        recv = self.get_names(channel)

        if recv.find ( " 353 "+config.bot_nick ) != -1:
            user_nicks = recv.split(':')[2].rstrip()
            if '+'+user in user_nicks.split() or '@'+user in user_nicks.split() or '%'+user in user_nicks.split():
                return True
            else:
                self.send_reply( ("No rights!"), user, channel )
                return False

    # Execute command
    def evalCommand(self, commandname, user, channel):
        try:
            imp.find_module('commands/'+commandname)
        except:
            return  #no such command
        imp.reload(eval(commandname))
        command_function=getattr(eval(commandname), commandname, None)
        if command_function != None:
            if inspect.isfunction(command_function):
                
                class TimedOut(Exception): # Raised if timed out.
                    pass

                def signal_handler(signum, frame):
                    raise TimedOut("Timed out!")

                signal.signal(signal.SIGALRM, signal_handler)

                signal.alarm(config.command_timeout)    #Limit command execution time
                try:
                    command_function(self, user, channel)
                    signal.alarm(0)
                except TimedOut as msg:
                    self.send_reply( ("Timed out!"), user, channel)

    def process_command(self, user, channel):
        command = (self.command)
        # Break the command into pieces, so we can interpret it with arguments
        command = command.split()

############    COMMADS:
        ### All public commands go here
        # The command isn't case sensitive
        if spam_filter.start(self, user, channel):
            # This line makes sure an actual command was sent, not a plain command prefix
            if ( len(command) == 0):
                error = "Usage: "+config.command_prefix+"command [arguments]"
                self.send_reply( (error), user, channel )
                return
            self.evalCommand(command[0].lower(), user, channel)
#####
class BotCrashed(Exception): # Raised if the bot has crashed.
    pass

def main():
    # Here begins the main programs flow:
    connect_class = IRC_Server(config.server, config.port, config.bot_nick, config.channels.split(','))
    run_connect_class = multiprocessing.Process(None,connect_class.connect,name="IRC Server" )
    run_connect_class.start()
    ### run notification process
    if ( config.notifications == True ):
        print("Run 'notifications' process...")
        run_notify = multiprocessing.Process(None,notifications.start(connect_class))
        run_notify.start()
    try:
        while(connect_class.should_reconnect):
            time.sleep(5)
        run_connect_class.join()
    except KeyboardInterrupt: # Ctrl + C pressed
        pass # We're ignoring that Exception, so the user does not see that this Exception was raised.
    if run_connect_class.is_alive:
        run_connect_class.terminate()
        run_connect_class.join() # Wait for terminate
    if run_connect_class.exitcode == 0 or run_connect_class.exitcode < 0:
        print("Bot exited.")
    else:
        raise BotCrashed("The bot has crashed")
