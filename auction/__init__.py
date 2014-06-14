

# login_data schema:
# CREATE TABLE "login_data"(id integer PRIMARY KEY, username text, hash blob, salt blob, can_talk integer, team text, color TEXT);
#
# team_data schema:
# CREATE TABLE "team_data"(id integer PRIMARY KEY, code text, name text, money int, withdrawn int default 0, color1 TEXT, color2 TEXT);
#
# player_data schema:
# CREATE TABLE "player_data"(id integer PRIMARY KEY, name text, retain_price int, tiers text, team text);


#from __future__ import unicode_literals
import sqlite3
import os
import scrypt
import json
import time
import datetime
import re
import threading
import random
#import struct
from flask import g, Flask, render_template, request, send_from_directory, redirect, Response, escape
from flask_sockets import Sockets


# constant definitions
BID_TIME = 300  # measured in 20ths of a second
MIN_PLAYERS = 10
MIN_BID = 3000
STARTING_MONEY = 100000

app = Flask(__name__)
#DATABASE = 'database.db'
sockets = Sockets(app)

#app.secret_key = 'R\xc3\n\x9d{%\xc2J\xf5\xb1\xdb\x9950\x0e\xf0\xed\x93\xf0[\x1em\xfd\x1d'
app.config.update(dict(DATABASE='database.db',))
#app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024


def connect_db():
    return sqlite3.connect(app.config['DATABASE'])

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = connect_db()
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()
        
        
def fix_team_data_ids(db, c):
    c.execute('select * from team_data;')
    rows = c.fetchall()
    count = 1
    for i in rows:
        c.execute('update team_data set id=? where code=?;', (count, i[1]))
        count += 1
    db.commit()
    
    
backlog = ['======\n']
admin_backlog = ['======\n']
connected_sockets = {}
username_colors = {}

team_colors = {} #TODO:

currently_bidding = False
timer_thread = None
lock = threading.Lock()
bid_timer = -1
bid_reset = False
player_being_bid_on = (None, None)

def timer_thread_function():
    global auction_state, bid_timer, bid_reset, player_being_bid_on, currently_bidding, team_money, top_bids, players_on_team, withdrawn_teams
    if bid_timer == -1 or not currently_bidding:
        return
    while bid_timer > 0:
        if bid_reset:
            with lock:
                bid_timer = BID_TIME - 1
                bid_reset = False
            send_all('T' + str(BID_TIME / 20))
        else:
            with lock:
                bid_timer -= 1
        time.sleep(0.05)
        if bid_timer == 100:
            send_all('T5')
            with lock:
                send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + "Five seconds remaning..."))
        if bid_reset: # double checking the reset here, so you can't snipe the bid in the last 1/20th of a second
            with lock:
                bid_timer = BID_TIME - 1
                bid_reset = False
            send_all('T' + str(BID_TIME / 20))
    # now bid_timer is definitely 0
    with lock:
        currently_bidding = False
        send_all('T0')
        largest_bid = max(top_bids.values())
        winning_team_code = [i for i, j in top_bids.iteritems() if j == largest_bid][0]
        players_on_team[winning_team_code].append(player_being_bid_on[1])
        with app.app_context():
            db = get_db()
            c = db.cursor()
            c.execute("select * from team_data where code=?;", (winning_team_code,))
            row = c.fetchone()
            #nominating_team = {'id': 1, 'direction': 1, 'name': None, 'longname': None}
            send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + '<strong>' + player_being_bid_on[1] + "</strong> was sold to " + team_colorify(row[2]) + " for " + str(largest_bid)))
            db = get_db()
            c = db.cursor()
            team_money[winning_team_code] = row[3] - largest_bid
            c.execute("update team_data set money=? where id=?;", (team_money[winning_team_code], row[0]))
            c.execute("update player_data set team=? where id=?;", (winning_team_code, player_being_bid_on[0]))
            if team_money[winning_team_code] < MIN_BID:
                send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + team_colorify(row[2]) + " does not have enough money to nominate another player, so they have been forcibly withdrawn"))
                c.execute("update team_data set withdrawn=1 where id=?;", (row[0],))
                withdrawn_teams.add(winning_team_code)
                update_hides_for_withdrawn_team(winning_team_code)
            db.commit()
            #c.execute("select count(*) from team_data;")
            #row = c.fetchone()
            c.execute("select * from team_data;")
            rows = c.fetchall()
        new_team_for_nomination(rows)

def append_to_log(message):
    # this is currently disabled, since it just randomly drops huge chunks of logs, which makes it basically useless
#    global backlog
#    backlog.append(message)
#    if len(backlog) > 50:
#        with open('log.txt', 'a') as f:
#            try:
#                f.write('\n'.join(backlog) + '\n')
#            except Exception as e:
#                print 'error in append_to_log: ' + str(e)
                # PANIC MODE, drop everything and hope that shit holds together
                # i still have no idea what causes this error, just randomly happens occasionally out of nowhere
                # specifically: "'ascii' codec can't encode characters in position 293-295: ordinal not in range(128)"
                # like seriously wtf
                #for i in xrange(len(backlog)):
                #    backlog[i] = unicode(backlog[i], errors='replace')
                #f.write('\n'.join(backlog) + '\n')
#                print 'PANIC MODE, did we survive?'
#        backlog = []
    return message

def append_to_admin_log(message):
    # this is currently disabled, since it just randomly drops huge chunks of logs, which makes it basically useless
#    global admin_backlog
#    admin_backlog.append(message)
#    if len(admin_backlog) > 30:
#        with open('admin_log.txt', 'a') as f:
#            f.write('\n'.join(admin_backlog) + '\n')
#        admin_backlog = []
    return message

def send_all(message):
    sockets_to_remove = set()
    #print connected_sockets
    for i in connected_sockets:
        try:
            i.send(message)
        except Exception as e:
            print "send_all exception " + str(e)
            sockets_to_remove.add(i)
    for i in sockets_to_remove:
        connected_sockets.pop(i, None)
        
def new_team_for_nomination(rows):
    global auction_state, team_money, top_bids, players_on_team, withdrawn_teams
    if nominating_team['id'] == len(players_on_team) and nominating_team['direction'] == 1:
        nominating_team['direction'] = -1
    elif nominating_team['id'] == 1 and nominating_team['direction'] == -1:
        nominating_team['direction'] = 1
    else:
        nominating_team['id'] += nominating_team['direction']
    for i in rows:
        #team_money[i[1]] = i[3]
        #team_players[i[1]] = []
        top_bids[i[1]] = 0
        if i[0] == nominating_team['id']:
            nominating_team['name'] = i[1]
            nominating_team['longname'] = i[2]
    while nominating_team['name'] in withdrawn_teams:
        send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + team_colorify(nominating_team['longname']) + " has already withdrawn, so they have been skipped"))
        if len(withdrawn_teams) == len(players_on_team): # checking if all teams have withdrawn
            send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + "All teams have withdrawn.  Auction over!"))
            auction_state = 0
            update_team_list()
            return
        if nominating_team['id'] == len(players_on_team) and nominating_team['direction'] == 1:
            nominating_team['direction'] = -1
        elif nominating_team['id'] == 1 and nominating_team['direction'] == -1:
            nominating_team['direction'] = 1
        else:
            nominating_team['id'] += nominating_team['direction']
        for i in rows:
            if i[0] == nominating_team['id']:
                nominating_team['name'] = i[1]
                nominating_team['longname'] = i[2]
                break
    #nominating_players = [i for i in tokens if tokens[i][2] == row[1]]
    #nominating_team['name'] = row[1]
    update_team_list()
    send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + team_colorify(nominating_team['longname']) + " is up to nominate"))
    nomination_request()
        
def update_user_list(ws=None):
    l = []
    for x in connected_sockets.values():
        l.append(None)
        for y in tokens.values():
            if x == y[0]:
                if y[1]:
                    l.pop()
                    l.append(colorify_name(x))
    if ws is None:
        send_all('u' + json.dumps(l))
    else:
        ws.send('u' + json.dumps(l))
        
def update_team_list(ws=None):
    global players_on_team
    with app.app_context():
        db = get_db()
        c = db.cursor()
        c.execute("select code, name, money from team_data;")
        t = 't' + json.dumps([[team_colorify(j), i, k, ', '.join(players_on_team[i] if players_on_team[i] else ['&nbsp;'])] for i, j, k, in c.fetchall()])
    if ws is None:
        send_all(t)
    else:
        ws.send(t)

def update_player_list(ws=None):
    with app.app_context():
        db = get_db()
        c = db.cursor()
        c.execute("select id, name from player_data where team is null;")
        t = 'p' + json.dumps(c.fetchall())
    if ws is None:
        send_all(t)
    else:
        ws.send(t)
                
def update_all_bids(ws):
    if top_bids:
        largest_bid = max(top_bids.values())
        winning_team_code = [i for i, j in top_bids.iteritems() if j == largest_bid][0]
        for i, x in top_bids.iteritems():
            if i != winning_team_code:
                ws.send('B' + i + str(x))
        ws.send('B' + winning_team_code + str(largest_bid))
        
def update_hides_for_withdrawn_team(team_code):
    for i, x in connected_sockets.iteritems():
        for _, y in tokens.iteritems():
            if x == y[0] and y[2] == team_code:
                i.send('h')
        
def update_bid_buttons():
    for i, x in connected_sockets.iteritems():
        for _, y in tokens.iteritems():
            if x == y[0] and y[2] and y[2] not in withdrawn_teams:
                i.send('b')
        
money_re = re.compile(r'[^\d.-]+')
def money(string):
    try:
        if string[-1].lower() == 'k':
            return int(float(money_re.sub('', string)) * 1000)
        return int(string)
    except:
        return 0
    
    
def colorify(string, color):
    return '<span style="color: #' + color + '">' + string + '</span>'
    
def colorify_name(username):
    c = username_colors.get(username, None)
    if c:
        return colorify(username, c)
    else:
        return username
    
team_color_picker = lambda x, y, z: colorify(x, y[0]) if z % 2 else colorify(x, y[1])
def team_colorify(team):
    return ' '.join([team_color_picker(s, team_colors[team], i) for i, s in enumerate(team.split(' '))])
            
tokens = {}

auction_state = 0
top_bids = {}
team_money = {}
nominating_team = {'id': 0, 'direction': 1, 'name': None, 'longname': ''}
#nominating_players = []
#need_to_check_nominating_players = False

def nomination_request():
    #for i, x in connected_sockets.iteritems():
    #    for _, y in tokens.iteritems():
    #        if y[0] == x and y[2] == nominating_team['name']:
    #            update_player_list(i)
    send_all('h')
    for i, x in connected_sockets.iteritems():
        for _, y in tokens.iteritems():
            if x == y[0] and y[2] == nominating_team['name']:
                update_player_list(i)
                #need_to_check_nominating_players = False
                
with app.app_context():
    db = get_db()
    c = db.cursor()
    c.execute("select code, withdrawn, name from team_data;")
    rows = c.fetchall()
    players_on_team = {i[0]: [] for i in rows}
    withdrawn_teams = {i[0] for i in rows if i[1]}
    team_long_names = {i[0]: i[2] for i in rows}
    c.execute("select name, team from player_data;")
    for i in c.fetchall():
        if i[1]:
            players_on_team[i[1]].append(i[0])
    c.execute("select name, color1, color2 from team_data;")
    team_colors = {i[0]: (i[1], i[2]) for i in c.fetchall()}


@sockets.route('/socket/login')
def login_socket(ws):
    while True:
        try:
            message = ws.receive()
            if message is None:
                print "recieved None message in login_socket, breaking"
                break
            #print message
            login_json = json.loads(message)
            with app.app_context():
                db = get_db()
                c = db.cursor()
                c.execute("select * from login_data where username = ?;", (login_json['username'],))
                row = c.fetchone()
                # schema: id, username, hash, salt, can_talk, team
            if row is None:
                ws.send("Error: Username does not exist")
                continue
            if buffer(scrypt.hash(login_json['password'].encode('utf-8'), str(row[3]))) != row[2]:
                ws.send("Error: Incorrect password")
                continue
            token = os.urandom(8).encode('base64')
            while token in tokens:
                token = os.urandom(8).encode('base64')
            duplicate = None
            for i, x in tokens.iteritems():
                if x[0] == row[1]:
                    duplicate = i
            tokens.pop(duplicate, None)
            if not row[6]:
                with app.app_context():
                    db = get_db()
                    c = db.cursor()
                    c.execute('update login_data set color=? where id=?', ("%0.2X" % random.randint(0, 127) + "%0.2X" % random.randint(0, 127) + "%0.2X" % random.randint(0, 127), row[0]))
                    db.commit()
                    c.execute("select * from login_data where id=?;", (row[0],))
                    row = c.fetchone()
            tokens[token] = (row[1], row[4], row[5], row[6])
            username_colors[row[1]] = row[6]
            if row[4] == 2:
                ws.send("Admin: " + token)
            else:
                ws.send("Token: " + token)
            #if row[5] == nominating_team['name']:
            #    need_to_check_nominating_players = True
            #    nomination_request()
        except Exception as e:
            print 'exception in login_socket: ' + str(e)
            break

@sockets.route('/socket/chat_message')
def chat_message_socket(ws):
    connected_sockets[ws] = None
    update_user_list(ws)
    update_team_list(ws)
    update_all_bids(ws)
    global bid_timer
    if bid_timer > 0:
        ws.send('T' + str(bid_timer / 20))
    #global need_to_check_nominating_players
    #if need_to_check_nominating_players:
    #    nomination_request()
    while True:
        try:
            message = ws.receive()
            if message is None:
                print "recieved None message in chat_message_socket, breaking"
                username = connected_sockets.pop(ws, None)
                can_talk = 0
                for i, x in tokens.iteritems():
                    if username == x[0]:
                        can_talk = x[1]
                        break
                if username is not None and can_talk:
                    send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + colorify_name(username) + " has left"))
                update_user_list()
                break
            print "message " + message
            message_json = json.loads(message)
            if len(message_json) > 2:
                global timer_thread, player_being_bid_on, currently_bidding, bid_reset, withdrawn_teams, players_on_team
                if message_json[1] == 'join':
                    user_data = tokens.get(message_json[0], (None, None))
                    if connected_sockets[ws] is None:
                        connected_sockets[ws] = user_data[0]
                    if user_data[0] is not None and user_data[1]:
                        send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + colorify_name(user_data[0]) + " has joined"))
                    update_user_list()
                elif message_json[1] == "allconnected":
                    team = tokens.get(message_json[0], (None, None, None))[2]
                    if team and bid_timer > 0:
                        ws.send('b')
                    if team and team == nominating_team['name']:
                        update_player_list(ws)
                elif message_json[1] == 'getplayer':
                    with app.app_context():
                        db = get_db()
                        c = db.cursor()
                        c.execute("select * from player_data where id=?;", (message_json[2],))
                        row = c.fetchone()
                        ws.send('g' + json.dumps((row[1], row[3])))
                elif message_json[1] == 'submitplayer':
                    user_data = tokens.get(message_json[0], (None, None, None))
                    if user_data[2] == nominating_team['name'] and auction_state == 1 and not currently_bidding: # we don't have to check whether they've withdrawn, because withdrawn teams will never be up for nomination
                        with lock:
                            currently_bidding = True # this is here to hopefully prevent race condition bullshit
                        with app.app_context():
                            db = get_db()
                            c = db.cursor()
                            c.execute("select * from player_data where id=? and team is null;", (message_json[2],))
                            row = c.fetchone()
                        if row:
                            if timer_thread:
                                timer_thread.join() # shouldn't be necessary, but making sure it's closed just in case
                            player_being_bid_on = (message_json[2], row[1])
                            send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + colorify_name(user_data[0]) + " has nominated <strong>" + row[1] + "</strong> for auction"))
                            bid_timer = BID_TIME
                            timer_thread = threading.Thread(target=timer_thread_function)
                            timer_thread.start()
                            send_all('T' + str(BID_TIME / 20))
                            send_all('P' + row[1])
                            update_bid_buttons()
                            with lock:
                                for i in team_money:
                                    if i == user_data[2]:
                                        top_bids[i] = MIN_BID
                                        send_all('B' + i + str(MIN_BID))
                                    else:
                                        top_bids[i] = 0
                                        send_all('B' + i + '0')
                        else:
                            print 'got an invalid submitplayer from ', user_data
                elif message_json[1] == 'bid':
                    user_data = tokens.get(message_json[0], (None, None, None))
                    if user_data[2] and currently_bidding and user_data[2] not in withdrawn_teams:
                        largest_bid = max(top_bids.values())
                        if len(message_json) > 3:
                            potential_bid = message_json[2] + largest_bid
                        else:
                            potential_bid = message_json[2]
                        if potential_bid <= largest_bid:
                            send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + colorify_name(user_data[0]) + "'s bid of " + str(potential_bid) + " was rejected: bid must be larger than current bid"))
                        elif team_money[user_data[2]] < potential_bid:
                            send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + colorify_name(user_data[0]) + "'s bid of " + str(potential_bid) + " was rejected: not enough money"))
                        elif team_money[user_data[2]] - potential_bid < (MIN_PLAYERS - len(players_on_team[user_data[2]]) - 1) * MIN_BID:
                            send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + colorify_name(user_data[0]) + "'s bid of " + str(potential_bid) + " was rejected: not enough money for minimum players"))
                        elif potential_bid % 500:
                            send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + colorify_name(user_data[0]) + "'s bid of " + str(potential_bid) + " was rejected: bid must be a multiple of 500"))
                        else:
                            top_bids[user_data[2]] = potential_bid
                            with lock:
                                bid_reset = True
                            send_all('B' + user_data[2] + str(potential_bid))
                            send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + colorify_name(user_data[0]) + " has bid " + str(potential_bid) + " (for " + team_colorify(team_long_names[user_data[2]]) + ")"))
                elif message_json[1] == 'withdraw':
                    user_data = tokens.get(message_json[0], (None, None, None))
                    if user_data[2] == nominating_team['name'] and auction_state == 1:
                        if len(players_on_team[user_data[2]]) < MIN_PLAYERS:
                            send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + team_colorify(nominating_team['longname']) + " cannot withdraw: not enough players (initiated by " + colorify_name(user_data[0]) + ")"))
                        else:
                            with app.app_context():
                                db = get_db()
                                c = db.cursor()
                                c.execute("update team_data set withdrawn=1 where code=?;", (user_data[2],))
                                db.commit()
                                c.execute("select * from team_data;")
                                rows = c.fetchall()
                            withdrawn_teams.add(user_data[2])
                            send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + team_colorify(team_long_names[user_data[2]]) + " has withdrawn from the auction (initiated by " + colorify_name(user_data[0]) + ")"))
                            update_hides_for_withdrawn_team(user_data[2])
                            new_team_for_nomination(rows)
            else:
                user_data = tokens.get(message_json[0], (None, None))
                if user_data[0] is None:
                    ws.send("Error: Invalid token (not logged in)")
                    continue
                if user_data[1] < 1:
                    ws.send("Error: You do not have permission to talk")
                    continue
                if connected_sockets[ws] is None:
                    connected_sockets[ws] = user_data[0]
                send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] &lt;') + colorify_name(user_data[0]) + "&gt; " + unicode(escape(message_json[1]).encode("utf-8"), "utf-8")))
        except Exception as e:
            print "chat_message_socket exception: " + str(e)
            temp = connected_sockets[ws]
            connected_sockets.pop(ws, None)
            if temp is not None:
                send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + colorify_name(temp) + " has left"))
            #print 'u' + json.dumps(connected_sockets.values())
            update_user_list()
            break
    
@sockets.route('/socket/admin')
def admin_socket(ws):
    while True:
        try:
            message = ws.receive()
            if message is None:
                print "recieved None message in admin_socket, breaking"
                break
            print "adminmessage " + message
            message_json = json.loads(message)
            user_data = tokens.get(message_json[0], (None,))
            username = user_data[0]
            if username is None:
                ws.send(append_to_admin_log("Error: Invalid token (not logged in)"))
                continue
            if user_data[1] != 2:
                ws.send(append_to_admin_log("Error: Not admin"))
                continue
            # okay now we know they are verified
            ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + username + '> ' + message_json[1]))
            line = message_json[1].split(' ', 1)
            global auction_state, nominating_team, team_money, currently_bidding
            try:
                if line[0] == "testo":
                    send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + colorify_name(username) + " used testo"))
                    ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'testo'))
                elif line[0] == "py":
                    exec compile(line[1], '', 'exec')
                    ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'done'))
                elif line[0] == "showall":
                    with app.app_context():
                        db = get_db()
                        c = db.cursor()
                        c.execute("select * from team_data;")
                        send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + "Showing all teams (initiated by " + colorify_name(username) + ")"))
                        for row in c.fetchall():
                            send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + row[2] + " (" + row[1] + "): " + str(row[3]) + " credits"))
                elif line[0] == "listusers":
                    ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + ', '.join([escape(i) for i in connected_sockets.values()])))
                elif line[0] == "setcolor":
                    s = line[1].split(' ', 1)
                    token = None
                    for i, x in tokens.iteritems():
                        print i, x
                        if x[0] == s[1]:
                            token = i
                    if token:
                        with app.app_context():
                            db = get_db()
                            c = db.cursor()
                            c.execute('update login_data set color=? where username=?', s)
                            db.commit()
                            c.execute("select * from login_data where username=?;", (s[1],))
                            row = c.fetchone()
                        tokens[token] = (row[1], row[4], row[5], row[6])
                        username_colors[row[1]] = row[6]
                        update_user_list()
                        ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'done'))
                    else:
                        ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'no such user'))
                elif line[0] == "forcelog":
                    with open('admin_log.txt', 'a') as f:
                        f.write('\n'.join(admin_backlog) + '\n')
                    admin_backlog = []
                    with open('log.txt', 'a') as f:
                        f.write('\n'.join(backlog) + '\n')
                    backlog = []
                    ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'done'))
                elif line[0] == "addmoney":
                    with app.app_context():
                        db = get_db()
                        c = db.cursor()
                        s = line[1].split(' ', 1)
                        c.execute("select * from team_data where code=?;", (s[0],))
                        row = c.fetchone()
                        if row is None:
                            ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'no such team'))
                        else:
                            c.execute("update team_data set money=? where id=?;", (row[3] + money(s[1]), row[0]))
                            db.commit()
                            team_money[s[0]] = row[3] + money(s[1])
                            ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'done'))
                            send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + colorify_name(username) + " added " + str(money(s[1])) + " credits to '" + row[2]) + "'")
                    update_team_list()      
                elif line[0] == "remmoney":
                    with app.app_context():
                        db = get_db()
                        c = db.cursor()
                        s = line[1].split(' ', 1)
                        c.execute("select * from team_data where code=?;", (s[0],))
                        row = c.fetchone()
                        if row is None:
                            ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'no such team'))
                        else:
                            c.execute("update team_data set money=? where id=?;", (row[3] - money(s[1]), row[0]))
                            db.commit()
                            team_money[s[0]] = row[3] - money(s[1])
                            ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'done'))
                            send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + colorify_name(username) + " removed " + str(money(s[1])) + " credits from '" + row[2]) + "'")
                    update_team_list()
                elif line[0] == "setmoney":
                    with app.app_context():
                        db = get_db()
                        c = db.cursor()
                        s = line[1].split(' ', 1)
                        c.execute("select * from team_data where code=?;", (s[0],))
                        row = c.fetchone()
                        if row is None:
                            ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'no such team'))
                        else:
                            c.execute("update team_data set money=? where id=?;", (money(s[1]), row[0]))
                            db.commit()
                            team_money[s[0]] = money(s[1])
                            ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'done'))
                            send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + colorify_name(username) + " set the money of '" + row[2]) + "' to " + str(money(s[1])) + " credits")
                    update_team_list()
                elif line[0] == "addteam":
                    with app.app_context():
                        db = get_db()
                        c = db.cursor()
                        s = line[1].split(' ', 2)
                        print s
                        c.execute("INSERT INTO team_data(code, name, money) VALUES (?, ?, ?);", (s[0], s[2], money(s[1])))
                        db.commit()
                    ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'done'))
                    send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + colorify_name(username) + " added team '" + s[2] + "', with code '" + s[0] + "' and " + str(money(s[1])) + " credits"))
                    update_team_list()
                elif line[0] == "delteam":
                    with app.app_context():
                        db = get_db()
                        c = db.cursor()
                        c.execute("select * from team_data where code=?;", (line[1],))
                        row = c.fetchone()
                        if row is None:
                            ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'no such team'))
                        else:
                            c.execute("delete from team_data where id=?;", (row[0],))
                            db.commit()
                            ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'done'))
                            send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + colorify_name(username) + " deleted team '" + row[2]) + "'")
                        fix_team_data_ids(db, c)
                        update_team_list()
                elif line[0] == "addplayer":
                    with app.app_context():
                        db = get_db()
                        c = db.cursor()
                        s = line[1].split(' ', 2)
                        print s
                        c.execute("INSERT INTO player_data(name, retain_price, tiers) VALUES (?, ?, ?);", (s[2], money(s[0]), s[1]))
                        db.commit()
                    ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'done'))
                    send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + colorify_name(username) + " added player '" + s[2] + "', with tier(s) '" + s[1] + "' and " + str(money(s[0])) + " credits retain cost"))
                    nomination_request()
                elif line[0] == "delplayer":
                    with app.app_context():
                        db = get_db()
                        c = db.cursor()
                        c.execute("select * from player_data where name=?;", (line[1],))
                        row = c.fetchone()
                        if row is None:
                            ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'no such player'))
                        else:
                            c.execute("delete from player_data where id=?;", (row[0],))
                            db.commit()
                            ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'done'))
                            send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + colorify_name(username) + " deleted player '" + row[1]) + "'")
                    nomination_request()
                elif line[0] == "listplayers":
                    with app.app_context():
                        db = get_db()
                        c = db.cursor()
                        c.execute("select * from player_data;")
                        for row in c.fetchall():
                            send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + row[1] + " -- tiers: " + row[3] + ", retain: " + str(row[2]) + ", team: " + str(row[4])))
                elif line[0] == "reseteverything":
                    with app.app_context():
                        db = get_db()
                        c = db.cursor()
                        c.execute("update player_data set team=NULL;")
                        c.execute("update team_data set withdrawn=0;")
                        c.execute("update team_data set money=?;", (STARTING_MONEY,))
                        db.commit()
                    update_team_list()
                    ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'done -- make sure to restart the server now'))
                elif line[0] == "addbidder":
                    s = line[1].split(' ', 1)
                    with app.app_context():
                        db = get_db()
                        c = db.cursor()
                        c.execute("update login_data set team=? where username=?;", s)
                        db.commit()
                    for i, x in tokens.iteritems():
                        if s[1] == x[0]:
                            tokens[i] = (x[0], x[1], s[0])
                            #break
                    ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'done'))
                    send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + colorify_name(username) + " assigned " + colorify_name(s[1]) + " to bid for " + team_long_names[s[0]]))
                    if currently_bidding:
                        update_bid_buttons()
                elif line[0] == "removebidder":
                    with app.app_context():
                        db = get_db()
                        c = db.cursor()
                        c.execute("update login_data set team=NULL where username=?;", (line[1],))
                        db.commit()
                    for i, x in tokens.iteritems():
                        if line[1] == x[0]:
                            tokens[i] = (x[0], x[1], None)
                            #break
                    ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'done'))
                    send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + colorify_name(username) + " removed " + colorify_name(line[1]) + "'s ability to bid"))
                    if currently_bidding:
                        update_bid_buttons()
                elif line[0] == "addvoice":
                    with app.app_context():
                        db = get_db()
                        c = db.cursor()
                        c.execute("update login_data set can_talk=1 where username=?;", (line[1],))
                        db.commit()
                    for i, x in tokens.iteritems():
                        if line[1] == x[0]:
                            tokens[i] = (x[0], 1, x[2])
                            #break
                    ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'done'))
                    send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + colorify_name(username) + " allowed " + colorify_name(line[1]) + " to talk"))
                    update_user_list()
                elif line[0] == "removevoice":
                    with app.app_context():
                        db = get_db()
                        c = db.cursor()
                        c.execute("update login_data set can_talk=0 where username=?;", (line[1],))
                        db.commit()
                    for i, x in tokens.iteritems():
                        if line[1] == x[0]:
                            tokens[i] = (x[0], 0, x[2])
                            #break
                    ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'done'))
                    send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + colorify_name(username) + " removed " + colorify_name(line[1]) + "'s ability to talk"))
                    update_user_list()
                elif line[0] == "endbid":
                    if currently_bidding:
                        with lock:
                            currently_bidding = False
                        send_all('T0')
                    send_all('h')
                    send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + "The bid has been ended and nomination controls have been hidden"))
                elif line[0] == "hideall":
                    send_all('h')
                    send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + "Nomination/bidding controls have been hidden"))
                elif line[0] == "reshownomination":
                    nomination_request()
                    send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + "Nomination controls have been unhidden"))
                elif line[0] == "nominationforward":
                    s = nominating_team['longname']
                    global top_bids, players_on_team, withdrawn_teams
                    if nominating_team['id'] == len(players_on_team) and nominating_team['direction'] == 1:
                        nominating_team['direction'] = -1
                    elif nominating_team['id'] == 1 and nominating_team['direction'] == -1:
                        nominating_team['direction'] = 1
                    else:
                        nominating_team['id'] += nominating_team['direction']
                    for i in rows:
                        top_bids[i[1]] = 0
                        if i[0] == nominating_team['id']:
                            nominating_team['name'] = i[1]
                            nominating_team['longname'] = i[2]
                    while nominating_team['name'] in withdrawn_teams:
                        send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + team_colorify(nominating_team['longname']) + " has already withdrawn, so they have been skipped"))
                        if len(withdrawn_teams) == len(players_on_team): # checking if all teams have withdrawn
                            send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + "All teams have withdrawn.  Auction over!"))
                            auction_state = 0
                            update_team_list()
                            return
                        if nominating_team['id'] == len(players_on_team) and nominating_team['direction'] == 1:
                            nominating_team['direction'] = -1
                        elif nominating_team['id'] == 1 and nominating_team['direction'] == -1:
                            nominating_team['direction'] = 1
                        else:
                            nominating_team['id'] += nominating_team['direction']
                        for i in rows:
                            if i[0] == nominating_team['id']:
                                nominating_team['name'] = i[1]
                                nominating_team['longname'] = i[2]
                                break
                    update_team_list()
                    nomination_request()
                    send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + team_colorify(s) + " has been skipped, moving forwards to " + team_colorify(nominating_team['longname'])))
                elif line[0] == "nominationbackward":
                    s = nominating_team['longname']
                    global top_bids, players_on_team, withdrawn_teams
                    if nominating_team['id'] == len(players_on_team) and nominating_team['direction'] == -1:
                        nominating_team['direction'] = 1
                    elif nominating_team['id'] == 1 and nominating_team['direction'] == 1:
                        nominating_team['direction'] = -1
                    else:
                        nominating_team['id'] += nominating_team['direction'] * -1
                    for i in rows:
                        top_bids[i[1]] = 0
                        if i[0] == nominating_team['id']:
                            nominating_team['name'] = i[1]
                            nominating_team['longname'] = i[2]
                    while nominating_team['name'] in withdrawn_teams:
                        send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + team_colorify(nominating_team['longname']) + " has already withdrawn, so they have been skipped"))
                        if len(withdrawn_teams) == len(players_on_team): # checking if all teams have withdrawn
                            send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + "All teams have withdrawn.  Auction over!"))
                            auction_state = 0
                            update_team_list()
                            return
                        if nominating_team['id'] == len(players_on_team) and nominating_team['direction'] == -1:
                            nominating_team['direction'] = 1
                        elif nominating_team['id'] == 1 and nominating_team['direction'] == 1:
                            nominating_team['direction'] = -1
                        else:
                            nominating_team['id'] += nominating_team['direction'] * -1
                        for i in rows:
                            if i[0] == nominating_team['id']:
                                nominating_team['name'] = i[1]
                                nominating_team['longname'] = i[2]
                                break
                    update_team_list()
                    nomination_request()
                    send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + team_colorify(s) + " has been skipped, moving backwards to " + team_colorify(nominating_team['longname'])))
                
                #TODO:
                elif line[0] == "startauction":
                    if auction_state != 0:
                        ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'error: auction is already in progress'))
                    else:
                        auction_state = 1
                        send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + colorify_name(username) + " started the auction"))
                        nominating_team = {'id': 1, 'direction': 1, 'name': None, 'longname': None}
                        with app.app_context():
                            db = get_db()
                            c = db.cursor()
                            c.execute("select * from team_data;")
                            rows = c.fetchall()
                            #c.execute("select * from team_data where id=?;", (nominating_team['id'],))
                            #row = c.fetchone()
                        for i in rows:
                            team_money[i[1]] = i[3]
                            #team_players[i[1]] = []
                            top_bids[i[1]] = 0
                            if i[0] == nominating_team['id']:
                                nominating_team['name'] = i[1]
                                nominating_team['longname'] = i[2]
                        #nominating_players = [i for i in tokens if tokens[i][2] == row[1]]
                        #nominating_team['name'] = row[1]
                        send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + team_colorify(nominating_team['longname']) + " is up to nominate"))
                        nomination_request()
                elif line[0] == "endauction":
                    if auction_state == 0:
                        ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'error: auction is not in progress'))
                    else:
                        auction_state = 0
                        send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + username + " ended the auction"))
                elif line[0] == "pauseauction":
                    if auction_state == 0:
                        ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'error: auction is not in progress'))
                    elif auction_state == 2:
                        ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'error: auction is already paused'))
                    else:
                        auction_state = 2
                        send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + username + " paused the auction"))
                elif line[0] == "resumeauction":
                    if auction_state == 0:
                        ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'error: auction is not in progress'))
                    elif auction_state == 1:
                        ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'error: auction is not paused'))
                    else:
                        auction_state = 1
                        send_all(append_to_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + username + " resumed the auction"))
                else:
                    ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'invalid command'))
            except Exception as e:
                ws.send(append_to_admin_log(datetime.datetime.utcnow().strftime('[%H:%M:%S] ') + 'some error occurred: ' + str(e)))
            update_team_list()
        except Exception as e:
            print "admin_socket exception: " + str(e)
            break
                
            
            
    
    
@app.route("/", methods=['GET'])
def page_index():
    return render_template('index.html')

@app.route("/register", methods=['GET', 'POST'])
def page_register():
    if request.method == 'POST':
        db = get_db()
        c = db.cursor()
        salt = buffer(os.urandom(16))
        password_hash = buffer(scrypt.hash(request.form['password'].encode('utf-8'), str(salt)))
        #team = request.form['team'].lower()
        #if team == '':
        #    team = None
        #elif len(team) != 3:
        #    return """<html><head><meta http-equiv="refresh" content="2;url=/register"></head><body>Invalid three letters</body></html>"""
        # later we'll want to readd this stuff up above, but not yet so just set team to null
        team = None
        username = request.form['username']
        c.execute('select id from login_data where username=?;', (username,))
        if c.fetchone() is not None:
            return """<html><head><meta http-equiv="refresh" content="2;url=/register"></head><body>User already exists</body></html>"""
        try:
            c.execute("INSERT INTO login_data(username, hash, salt, can_talk, team, color) VALUES (?, ?, ?, ?, ?, ?);", (username, password_hash, salt, 0, team, None))
        except Exception as e:
            print e
        db.commit()
        return """<html><head><meta http-equiv="refresh" content="2;url=/"></head><body>Registration successful</body></html>"""
    else:
        return render_template('register.html')

if __name__ == "__main__":
    app.run(debug=True)

