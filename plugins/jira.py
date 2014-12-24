from neb.engine import KeyValueStore
from neb.plugins import Plugin

import getpass
import json
import re
import requests

import logging as log


class JiraPlugin(Plugin):
    """ Plugin for interacting with JIRA.
    jira version : Display version information for this platform.
    jira track <project> <project2> ... : Track multiple projects
    jira expand <project> <project2> ... : Expand issue IDs for the given projects with issue information.
    jira stop track|tracking : Stops tracking for all projects.
    jira stop expand|expansion|expanding : Stop expanding jira issues.
    jira show track|tracking : Show which projects are being tracked.
    jira show expansion|expand|expanding : Show which project keys will result in issue expansion.
    """
    name = "jira"
    
    TRACK = ["track", "tracking"]
    EXPAND = ["expansion", "expand", "expanding"]
    
    # New events:
    #    Type: org.matrix.neb.plugin.jira.issues.display
    #    State: Yes
    #    Content: {
    #        display: [projectKey1, projectKey2, ...]
    #    }

    def __init__(self, *args, **kwargs):
        super(JiraPlugin, self).__init__(*args, **kwargs)
        self.store = KeyValueStore("jira.json")

        if not self.store.has("url"):
            url = raw_input("JIRA URL: ").strip()
            self.store.set("url", url)

        if not self.store.has("user") or not self.store.has("pass"):
            user = raw_input("(%s) JIRA Username: " % self.store.get("url")).strip()
            pw = getpass.getpass("(%s) JIRA Password: " % self.store.get("url")).strip()
            self.store.set("user", user)
            self.store.set("pass", pw)

        self.state = {
            # room_id : { display: [projectKey1, projectKey2, ...] }
        }

        self.auth = (self.store.get("user"), self.store.get("pass"))
        self.regex = re.compile(r"\b(([A-Za-z]+)-\d+)\b")

    def jira(self, event, args):
        # TODO: make this configurable
        if event["user_id"] not in self.matrix.config.admins:
            return self._body("Sorry, only %s can do that." % json.dumps(self.matrix.config.admins))

        try:
            return actions[action](event, args)
        except KeyError:
            return self._body("Unknown JIRA action: %s" % action)

    def cmd_stop(self, event, action):
        if action in self.TRACK:
            self.matrix.send_event(
                event["room_id"],
                "org.matrix.neb.plugin.jira.issues.display",
                {
                    "display": []
                },
                state=True
            )
            self._send_display_event(event["room_id"], [])

            url = self.store.get("url")
            return "Stopped tracking project keys from %s." % (url)
        elif action in self.EXPAND:
            self.matrix.send_event(
                event["room_id"],
                "org.matrix.neb.plugin.jira.issues.display",
                {
                    "display": []
                },
                state=True
            )
            self._send_display_event(event["room_id"], [])

            url = self.store.get("url")
            return "Stopped expanding project keys from %s." % (url)
        else:
            return "Unknown stop command"

    def cmd_track(self, event, *args):
        if not args:
            try:
                return self._body("Currently tracking %s" % json.dumps(self.state[event["room_id"]]["display"]))
            except KeyError:
                return self._body("Not tracking any projects currently.")

                
        args = [k.upper() for k in args]
        for key in args:
            # FIXME: This accepts commas
            if not re.match("[A-Z][A-Z_0-9]+", key):
                return self._body("Key %s isn't a valid project key." % key)

        self._send_display_event(event["room_id"], args)


        url = self.store.get("url")
        return "Issues for projects %s from %s will be displayed as they are mentioned/edited." % (args, url)
        
    def cmd_version(self, event):
        url = self._url("/rest/api/2/serverInfo")
        response = json.loads(requests.get(url).text)

        info = "%s : version %s : build %s" % (response["serverTitle"],
               response["version"], response["buildNumber"])

        return info

    def _send_display_event(self, room_id, project_keys):
        self.matrix.send_event(
            room_id,
            "org.matrix.neb.plugin.jira.issues.display",
            {
                "display": project_keys
            },
            state=True
        )

    

    def on_msg(self, event, body):
        room_id = event["room_id"]
        body = body.upper()
        groups = self.regex.findall(body)
        if not groups:
            return

        projects = []
        try:
            projects = self.state[room_id]["display"]
        except KeyError:
            return

        for (key, project) in groups:
            if project in projects:
                try:
                    issue_info = self._get_issue_info(key)
                    if issue_info:
                        self.matrix.send_message(
                            event["room_id"],
                            self._body(issue_info)
                        )
                except Exception as e:
                    log.exception(e)

    def on_event(self, event, event_type):
        if event_type == "org.matrix.neb.plugin.jira.issues.display":
            self._set_display_event(event)

    def on_receive_jira_push(self, info):
        log.debug("on_recv %s", info)
        project = self.regex.match(info["key"]).groups()[1]

        # form the message
        link = "%s/browse/%s" % (self.store.get("url"), info["key"])
        push_message = "%s %s %s - %s %s" % (info["user"], info["action"],
                       info["key"], info["summary"], link)

        # send messages to all rooms registered with this project.
        for (room_id, room_info) in self.state.iteritems():
            try:
                if project in room_info["display"]:
                    self.matrix.send_message(room_id, self._body(push_message))
            except KeyError:
                pass

    def _set_display_event(self, event):
        room_id = event["room_id"]
        issues = event["content"]["display"]

        if room_id not in self.state:
            self.state[room_id] = {}

        if type(issues) == list:
            self.state[room_id]["display"] = issues
        else:
            self.state[room_id]["display"] = []

    def on_sync(self, sync):

        for room in sync["rooms"]:
            # see if we know anything about these rooms
            room_id = room["room_id"]
            if room["membership"] != "join":
                continue

            self.state[room_id] = {}

            try:
                for state in room["state"]:
                    if state["type"] == "org.matrix.neb.plugin.jira.issues.display":
                        self._set_display_event(state)
            except KeyError:
                pass

        log.debug("Plugin: JIRA Sync state:")
        log.debug(json.dumps(self.state, indent=4))

    def _get_issue_info(self, issue_key):
        url = self._url("/rest/api/2/issue/%s" % issue_key)
        res = requests.get(url, auth=self.auth)
        if res.status_code != 200:
            return

        response = json.loads(res.text)
        link = "%s/browse/%s" % (self.store.get("url"), issue_key)
        desc = response["fields"]["summary"]
        status = response["fields"]["status"]["name"]
        priority = response["fields"]["priority"]["name"]
        reporter = response["fields"]["reporter"]["displayName"]
        assignee = ""
        if response["fields"]["assignee"]:
            assignee = response["fields"]["assignee"]["displayName"]

        info = "%s : %s [%s,%s,reporter=%s,assignee=%s]" % (link, desc, status,
               priority, reporter, assignee)
        return info

    def _url(self, path):
        return self.store.get("url") + path

    def get_webhook_key(self):
        return "jira"

    def on_receive_webhook(self, url, data, ip, headers):
        j = json.loads(data)

        info = self.get_webhook_json_keys(j)
        self.on_receive_jira_push(info)

    def get_webhook_json_keys(self, j):
        key = j['issue']['key']
        user = j['user']['name']
        self_key = j['issue']['self']
        summary = self.get_webhook_summary(j)
        action = ""

        if j['webhookEvent'] == "jira:issue_updated":
            action = "updated"
        elif j['webhookEvent'] == "jira:issue_deleted":
            action = "deleted"
        elif j['webhookEvent'] == "jira:issue_created":
            action = "created"

        return {
            "key": key,
            "user": user,
            "summary": summary,
            "self": self_key,
            "action": action
        }

    def get_webhook_summary(self, j):
        summary = j['issue']['fields']['summary']
        priority = j['issue']['fields']['priority']['name']
        status = j['issue']['fields']['status']['name']

        if "resolution" in j['issue']['fields'] \
            and j['issue']['fields']['resolution'] is not None:
            status = "%s (%s)" \
                % (status, j['issue']['fields']['resolution']['name'])

        return "%s [%s, %s]" \
            % (summary, priority, status)
