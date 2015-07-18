#!/usr/bin/env python3
"""
* yuu.py
* Author: stal, stqism; April 2014
* Copyright (c) 2014 Zodiac Labs.
* Further licensing information: see LICENSE.
"""
import tornado.ioloop
import tornado.httpserver
import tornado.web
import tornado.log
import os
import json
import nacl.public as public
import nacl.signing as signing
import nacl.encoding
import nacl.exceptions
import database
import datetime
import time
import logging
import re
import pwd
import grp
import sys
import random
import hashlib
import urllib.parse as parse
from collections import Counter, defaultdict

import error_codes
import barcode
import dns_discovery
import dns_serve
import hooks

tornado.log.enable_pretty_logging()
LOGGER = logging.getLogger("yuu")

ACTION_PUBLISH   = 1
ACTION_UNPUBLISH = 2
ACTION_LOOKUP    = 3
INVOKABLE_ACTIONS = {ACTION_PUBLISH, ACTION_UNPUBLISH, ACTION_LOOKUP}
THROTTLE_THRESHOLD = 5

VALID_KEY = re.compile(r"^[A-Fa-f0-9]{64}$")
VALID_ID  = re.compile(r"^[A-Fa-f0-9]{76}$")
REMOVE_NEWLINES = re.compile("[\r\n]+")
DISALLOWED_CHARS = set(" @/:;()\"'")
DISALLOWED_NAMES = {}
NAME_LIMIT_HARD  = 63
BIO_LIMIT        = 250 # fixme this should be configurable

ENTRIES_PER_PAGE = 10

#pragma mark - crypto

SIGNATURE_ENC = nacl.encoding.Base64Encoder
KEY_ENC = nacl.encoding.HexEncoder
STORE_ENC = nacl.encoding.HexEncoder

class CryptoCore(object):
    def __init__(self):
        """Load or initialize crypto keys."""
        try:
            with open("key", "rb") as keys_file:
                keys = keys_file.read()
        except IOError:
            keys = None
        if keys:
            self.pkey = public.PrivateKey(keys, STORE_ENC)
            self.skey = signing.SigningKey(keys, STORE_ENC)
        else:
            kp = public.PrivateKey.generate()
            with open("key", "wb") as keys_file:
                keys_file.write(kp.encode(STORE_ENC))
            self.pkey = kp
            self.skey = signing.SigningKey(bytes(self.pkey),
                                           nacl.encoding.RawEncoder)

    def sign(self, uobj):
        e = nacl.encoding.HexEncoder
        pubkey = e.decode(uobj.public_key)
        pin = e.decode(uobj.pin) if uobj.pin else b""
        checksum = e.decode(uobj.checksum)
        name = uobj.name.encode("utf8")

        text = b"".join((name, pubkey, pin, checksum))
        return self.skey.sign(text, encoder=SIGNATURE_ENC).decode("utf8")

    @staticmethod
    def compute_checksum(data, iv=(0, 0)):
        e = nacl.encoding.HexEncoder
        checksum = list(iv)
        for ind, byte in enumerate(e.decode(data)):
            checksum[ind % 2] ^= byte
        return "".join(hex(byte)[2:].zfill(2) for byte in checksum).upper()

    @property
    def public_key(self):
        return self.pkey.public_key.encode(KEY_ENC).decode("utf8").upper()

    @property
    def verify_key(self):
        return self.skey.verify_key.encode(KEY_ENC).decode("utf8").upper()

    def dsrep_decode_name(self, client, nonce, pl):
        box = public.Box(self.pkey, public.PublicKey(client))
        by = box.decrypt(pl, nonce)
        return by

    def dsrec_encrypt_key(self, client, nonce, msg):
        box = public.Box(self.pkey, public.PublicKey(client))
        by = box.encrypt(msg, nonce)
        return by[24:]
        
#pragma mark - web

CONSONANTS = "bcdfghjklmnpqrstvwxyz"
VOWELS     = "aeiou"

def new_password():
    def sylfunc():
        return "".join([random.choice(CONSONANTS), random.choice(VOWELS),
                        random.choice(CONSONANTS)])
    return "-".join(
        [sylfunc() for x in range(random.randint(4, 6))]
    )

class HTTPSPolicyEnforcer(tornado.web.RequestHandler):
    def _fail(self):
        self.set_status(400)
        self.write(error_codes.ERROR_NOTSECURE)
        return ""

    post = get = _fail

class APIHandler(tornado.web.RequestHandler):
    RETURNS_JSON = 1
    
    @staticmethod
    def _typecheck_dict(envelope, expect):
        for key, value in expect.items():
            if not isinstance(envelope.get(key), value):
                LOGGER.warn("typecheck failed on json")
                return 0
        return 1

    def _encrypted_payload_prologue(self, envelope):
        if not self._typecheck_dict(envelope, {"public_key": str, "nonce": str,
                                               "encrypted": str}):
            self.set_status(400)
            self.json_payload(error_codes.ERROR_BAD_PAYLOAD)
            return
        try:
            other_key = public.PublicKey(envelope["public_key"], KEY_ENC)
        except nacl.exceptions.CryptoError:
            LOGGER.warn("did fail req because other pk was bad")
            self.set_status(400)
            self.json_payload(error_codes.ERROR_BAD_PAYLOAD)
            return

        box = public.Box(self.settings["crypto_core"].pkey, other_key)

        try:
            nonce = nacl.encoding.Base64Encoder.decode(envelope["nonce"])
            ciphertext = nacl.encoding.Base64Encoder.decode(envelope["encrypted"])
            clear = box.decrypt(ciphertext, nonce, nacl.encoding.RawEncoder)
        except (ValueError, TypeError, nacl.exceptions.CryptoError):
            LOGGER.warn("did fail req because a base64 value was bad")
            self.set_status(400)
            self.json_payload(error_codes.ERROR_BAD_PAYLOAD)
            return

        try:
            clear = json.loads(clear.decode("utf8"))
        except (UnicodeDecodeError, TypeError):
            LOGGER.warn("did fail req because inner json decode failed")
            self.set_status(400)
            self.json_payload(error_codes.ERROR_BAD_PAYLOAD)
            return
        return clear

    def json_payload(self, payload):
        if self.RETURNS_JSON:
            self.write(payload)
        else:
            self.render("api_error_pretty.html", payload=payload,
                        f=error_codes.DESCRIPTIONS[payload["c"]])

    def update_db_entry(self, auth, name, pub, bio, check, privacy, pin=None,
                        password=None):
        dbc = self.settings["local_store"]
        with dbc.lock:
            session, owner_of_cid = dbc.get_by_id_ig(pub)
            if owner_of_cid and owner_of_cid.name != name:
                session.close()
                self.set_status(400)
                self.json_payload(error_codes.ERROR_DUPE_ID)
                return 0
        
            session, mus = dbc.get_ig(name, session)
            if not mus:
                mus = database.User()
            elif mus.public_key != auth:
                session.close()
                self.set_status(400)
                self.json_payload(error_codes.ERROR_NAME_TAKEN)
                return 0
        
            mus.name = name
            mus.public_key = pub
            mus.checksum = check
            mus.privacy = privacy
            mus.timestamp = datetime.datetime.now()
            mus.sig = self.settings["crypto_core"].sign(mus)
            mus.bio = bio
            mus.pin = pin
            if password:
                mus.password = password
            ok = dbc.update_atomic(mus, session)
            if not ok:
                session.close()
                self.set_status(400)
                self.json_payload(error_codes.ERROR_DUPE_ID)
                return 0
            if hooks:
                hooks.did_update_record(self.settings["hooks_state"],
                                        database.StaleUser(mus))
            session.close()
        return 1

class APIUpdateName(APIHandler):
    def initialize(self, envelope):
        self.envelope = envelope

    def post(self):
        ctr = self.settings["address_ctr"][ACTION_PUBLISH]
        if ctr["clear_date"][self.request.remote_ip] < time.time():
            del ctr["counter"][self.request.remote_ip]
            del ctr["clear_date"][self.request.remote_ip]
        ctr["counter"][self.request.remote_ip] += 1
        # Clears in one hour
        ctr["clear_date"][self.request.remote_ip] = time.time() + 3600
        
        if ctr["counter"][self.request.remote_ip] > THROTTLE_THRESHOLD:
            self.set_status(400)
            self.write(error_codes.ERROR_RATE_LIMIT)
            return
        
        clear = self._encrypted_payload_prologue(self.envelope)
        if not clear:
            return

        if not self._typecheck_dict(clear, {"tox_id": str, "name": str,
                                            "timestamp": int, "privacy": int,
                                            "bio": str}):
            self.set_status(400)
            self.write(error_codes.ERROR_BAD_PAYLOAD)
            return

        auth = self.envelope["public_key"].upper()
        id_ = clear["tox_id"].upper()
        name = clear["name"].lower()
        bio = REMOVE_NEWLINES.sub(" ", clear["bio"].strip())
        ctime = int(time.time())

        if (not VALID_ID.match(id_)
            or not set(name).isdisjoint(DISALLOWED_CHARS)
            or abs(ctime - clear["timestamp"]) > 300
            or name in DISALLOWED_NAMES
            or len(name) > NAME_LIMIT_HARD
            or len(bio) > BIO_LIMIT):
            self.set_status(400)
            self.json_payload(error_codes.ERROR_BAD_PAYLOAD)
            return

        pub, pin, check = id_[:64], id_[64:72], id_[72:]

        old_rec = self.settings["local_store"].get(name)
        if not old_rec:
            salt = os.urandom(16)
            password = new_password()
            hash_ = salt + hashlib.sha512(salt + password.encode("ascii")).digest()
        else:
            password = None
            hash_ = None

        if self.update_db_entry(auth, name, pub, bio, check,
                                max(clear["privacy"], 0), pin, hash_):
            ok = error_codes.ERROR_OK.copy()
            ok["password"] = password
            self.json_payload(ok)
        return

class APIReleaseName(APIHandler):
    def initialize(self, envelope):
        self.envelope = envelope

    def post(self):
        clear = self._encrypted_payload_prologue(self.envelope)
        if not clear:
            return

        ctime = int(time.time())
        pk = clear.get("public_key", "").upper()
        if (not VALID_KEY.match(pk)
            or abs(ctime - clear.get("timestamp", 0)) > 300):
            self.set_status(400)
            self.json_payload(error_codes.ERROR_BAD_PAYLOAD)
            return

        rec = self.settings["local_store"].get_by_id_ig(pk)[1]
        old = database.StaleUser(rec)
        self.settings["local_store"].delete_pk(pk)
        self.json_payload(error_codes.ERROR_OK)
        if hooks:
            hooks.did_delete_record(self.settings["hooks_state"], old)
        return

class APILookupID(tornado.web.RequestHandler):
    def initialize(self, envelope):
        self.envelope = envelope

    def _results(self, result):
        self.set_status(200 if result["c"] == 0 else 400)
        self.write(result)
        self.finish()

    def _build_local_result(self, name):
        rec = self.settings["local_store"].get(name)
        if not rec:
            return error_codes.ERROR_NO_USER
        base_ret = {
            "c": 0,
            "name": rec.name,
            "regdomain": self.settings["home"],
            "public_key": rec.tox_id(),
            "url": "tox://{0}@{1}".format(rec.name, self.settings["home"]),
            "verify": {
                "status": dns_discovery.SIGNSTATUS_GOOD,
                "detail": "Good (signed by local authority)",
            },
            "source": dns_discovery.SOURCE_LOCAL,
            "version": "Tox V1 (local)"
        }
        return base_ret

    @tornado.web.asynchronous
    def post(self):
        name = self.envelope.get("name").lower()
        if not name or name.endswith("@") or name.startswith("@"):
            self.set_status(400)
            self.write(error_codes.ERROR_BAD_PAYLOAD)
            self.finish()
            return
        if "@" not in name:
            name = "@".join((name, self.settings["home"]))
        user, domain = name.rsplit("@", 1)
        if domain == self.settings["home"]:
            self._results(self._build_local_result(user))
            return
        else:
            self.settings["lookup_core"].dispatch_lookup(name, self._results)

class APIFailure(APIHandler):
    def get(self):
        self.set_status(400)
        self.json_payload(error_codes.ERROR_METHOD_UNSUPPORTED)
        return

    def post(self):
        self.set_status(400)
        self.json_payload(error_codes.ERROR_BAD_PAYLOAD)
        return

def _make_handler_for_api_method(application, request, **kwargs):
    if request.protocol != "https":
        return HTTPSPolicyEnforcer(application, request, **kwargs)

    if request.method != "POST":
        return APIFailure(application, request, **kwargs)

    try:
        envelope = request.body.decode("utf8")
        envelope = json.loads(envelope)
        if envelope.get("action", -1) not in INVOKABLE_ACTIONS:
            raise TypeError("blah blah blah exceptions are flow control")
    except (UnicodeDecodeError, TypeError):
        LOGGER.warn("failing request because of an invalid first payload")
        return APIFailure(application, request, **kwargs)

    action = envelope.get("action")
    if action == ACTION_PUBLISH:
        return APIUpdateName(application, request, envelope=envelope)
    elif action == ACTION_UNPUBLISH:
        return APIReleaseName(application, request, envelope=envelope)
    elif action == ACTION_LOOKUP:
        return APILookupID(application, request, envelope=envelope)

class PublicKey(tornado.web.RequestHandler):
    def get(self):
        if self.request.protocol != "https":
            self.write(error_codes.ERROR_NOTSECURE)
        else:
            self.write({
                "c": 0,
                "key": self.settings["crypto_core"].public_key
            })

class CreateQR(tornado.web.RequestHandler):
    def _fail(self):
        self.set_status(404)
        return

    def get(self, path_id):
        if self.request.protocol != "https":
            self.write(error_codes.ERROR_NOTSECURE)
            return
        name = (parse.unquote(path_id) if path_id else "").lower()
        if not name or not set(name).isdisjoint(DISALLOWED_CHARS):
            return self._fail()
        rec = self.settings["local_store"].get(name)
        if not rec:
            return self._fail()

        self.set_header("Cache-Control", "public; max-age=86400")
        self.set_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.write(barcode.QRImage.get("@".join((name, self.settings["home"]))))
        return

class LookupAndOpenUser(tornado.web.RequestHandler):
    def _user_id(self):
        spl = self.request.host.rsplit(".", 1)[0]
        if spl == self.request.host:
            return None
        else:
            return spl

    def _render_open_user(self, name):
        if not name or not set(name).isdisjoint(DISALLOWED_CHARS):
            self.set_status(404)
            self.render("fourohfour.html", record=name,
                        realm=self.settings["home"])
            return

        rec = self.settings["local_store"].get(name)
        if not rec:
            self.set_status(404)
            self.render("fourohfour.html", record=name,
                        realm=self.settings["home"])
            return

        self.render("onemomentplease.html", record=rec,
                    realm=self.settings["home"])

    def _lookup_home(self):
        self.render("lookup_home.html")

    def get(self, path_id=None):
        if self.request.protocol != "https":
            self.write(error_codes.ERROR_NOTSECURE)
            return
        name = (parse.unquote(path_id) if path_id else "").lower()
        if name:
            return self._render_open_user(name)
        else:
            return self._lookup_home()

class FindFriends(tornado.web.RequestHandler):
    def _render_page(self, num):
        num = int(num)
        results = self.settings["local_store"].get_page(num,
                                                        ENTRIES_PER_PAGE)
        if not results:
            self.set_status(404)
            self.render("fourohfour.html", record="",
                        realm=self.settings["home"])
            return
        self.render("public_userlist.html", results_set=results,
                    realm=self.settings["home"],
                    next_page=(None if len(results) < ENTRIES_PER_PAGE
                                    else num + 1),
                    previous_page=(num - 1) if num > 0 else None)
    
    def get(self, page):
        if self.request.protocol != "https":
            self.write(error_codes.ERROR_NOTSECURE)
            return
        
        return self._render_page(page)

class EditKeyWeb(APIHandler):
    RETURNS_JSON = 0

    def get(self):
        if self.request.protocol != "https":
            self.json_payload(error_codes.ERROR_NOTSECURE)
            return
        self.render("edit_ui.html")

    def post(self):
        if self.request.protocol != "https":
            self.json_payload(error_codes.ERROR_NOTSECURE)
            return
    
        ctr = self.settings["address_ctr"][ACTION_PUBLISH]
        if ctr["clear_date"][self.request.remote_ip] < time.time():
            del ctr["counter"][self.request.remote_ip]
            del ctr["clear_date"][self.request.remote_ip]
        ctr["counter"][self.request.remote_ip] += 1
        # Clears in one hour
        ctr["clear_date"][self.request.remote_ip] = time.time() + 3600
    
        if ctr["counter"][self.request.remote_ip] > THROTTLE_THRESHOLD:
            self.set_status(400)
            return
    
        name = self.get_body_argument("name", "").lower()
        password = self.get_body_argument("password", "").lower()
        rec = self.settings["local_store"].get(name)
        if not (rec and rec.is_password_matching(password)):
            self.set_status(400)
            self.json_payload(error_codes.ERROR_BAD_PASSWORD)
            return

        action = self.get_body_argument("edit_action", "")
        if action not in {"Delete", "Update"}:
            self.set_status(400)
            self.json_payload(error_codes.ERROR_BAD_PAYLOAD)
            return
        elif action == "Delete":
            self.settings["local_store"].delete_pk(rec.public_key)
            self.redirect("/friends/0")
            return

        bio = self.get_body_argument("bio", "") or rec.bio
        if len(bio) > BIO_LIMIT:
            self.set_status(400)
            self.json_payload(error_codes.ERROR_BAD_PAYLOAD)
            return
        toxid = (self.get_body_argument("tox_id", "") or rec.tox_id()).upper()
        if not VALID_ID.match(toxid):
            self.set_status(400)
            self.json_payload(error_codes.ERROR_BAD_PAYLOAD)
            return
        privacy = 0 if self.get_body_argument("privacy", "off") == "on" else 1
    
        pkey = toxid[:64]
        pin = toxid[64:72]
        check = toxid[72:]
        if CryptoCore.compute_checksum("".join((pkey, pin))) != check:
            self.set_status(400)
            self.json_payload(error_codes.ERROR_BAD_PAYLOAD)
            return

        if self.update_db_entry(rec.public_key, name, pkey, bio, check, privacy, pin):
            self.redirect("/friends/0")
        return

class AddKeyWeb(APIHandler):
    RETURNS_JSON = 0

    def get(self):
        if self.request.protocol != "https":
            self.json_payload(error_codes.ERROR_NOTSECURE)
            return
        self.render("add_ui.html")

    def post(self):
        if self.request.protocol != "https":
            self.json_payload(error_codes.ERROR_NOTSECURE)
            return

        ctr = self.settings["address_ctr"][ACTION_PUBLISH]
        if ctr["clear_date"][self.request.remote_ip] < time.time():
            del ctr["counter"][self.request.remote_ip]
            del ctr["clear_date"][self.request.remote_ip]
        ctr["counter"][self.request.remote_ip] += 1
        # Clears in one hour
        ctr["clear_date"][self.request.remote_ip] = time.time() + 3600

        if ctr["counter"][self.request.remote_ip] > THROTTLE_THRESHOLD:
            self.set_status(400)
            return

        name = self.get_body_argument("name", "").lower()
        if (not DISALLOWED_CHARS.isdisjoint(set(name))
            or name in DISALLOWED_NAMES):
            self.set_status(400)
            self.json_payload(error_codes.ERROR_BAD_PAYLOAD)
            return

        bio = self.get_body_argument("bio", "")
        if len(bio) > BIO_LIMIT:
            self.set_status(400)
            self.json_payload(error_codes.ERROR_BAD_PAYLOAD)
            return

        toxid = self.get_body_argument("tox_id", "").upper()
        if not VALID_ID.match(toxid):
            self.set_status(400)
            self.json_payload(error_codes.ERROR_BAD_PAYLOAD)
            return

        privacy = 0 if self.get_body_argument("privacy", "off") == "on" else 1

        pkey = toxid[:64]
        pin = toxid[64:72]
        check = toxid[72:]
        if CryptoCore.compute_checksum("".join((pkey, pin))) != check:
            self.set_status(400)
            self.json_payload(error_codes.ERROR_BAD_PAYLOAD)
            return

        old_rec = self.settings["local_store"].get(name)
        if not old_rec:
            salt = os.urandom(16)
            password = new_password()
            hash_ = salt + hashlib.sha512(salt + password.encode("utf8")).digest()
        else:
            self.set_status(400)
            self.json_payload(error_codes.ERROR_NAME_TAKEN)
            return

        if self.update_db_entry(None, name, pkey, bio, check, privacy, pin,
                                hash_):
            self.render("addkeyweb_success.html", n=name, p=password,
                        regdomain=self.settings["home"])
        return

def main():
    with open("config.json", "r") as config_file:
        cfg = json.load(config_file)

    ioloop = tornado.ioloop.IOLoop.instance()
    crypto_core = CryptoCore()
    local_store = database.Database(cfg["database_url"])
    lookup_core = dns_discovery.DNSCore(cfg["number_of_workers"])
    lookup_core.callback_dispatcher = lambda cb, r: ioloop.add_callback(cb, r)
    if hooks:
        hooks_state = hooks.init(cfg, local_store)
    else:
        hooks_state = None

    # an interesting object structure
    address_ctr = {ACTION_PUBLISH: {"counter": Counter(),
                                    "clear_date": defaultdict(lambda: 0)}}

    LOGGER.info("API public key: {0}".format(crypto_core.public_key))
    LOGGER.info("Record sign key: {0}".format(crypto_core.verify_key))

    templates_dir = "_".join(("templates", cfg["templates"]))
    handlers = [("/api", _make_handler_for_api_method),
        ("/pk", PublicKey),
        (r"/barcode/(.+)\.svg$", CreateQR),
        (r"/u/(.+)?$", LookupAndOpenUser),
        (r"^/$", LookupAndOpenUser)
    ]
    if cfg["findfriends_enabled"]:
        handlers.append((r"/friends/([0-9]+)$", FindFriends))
        handlers.append((r"/add_ui", AddKeyWeb))
        handlers.append((r"/edit_ui", EditKeyWeb))
    app = tornado.web.Application(
        handlers,
        template_path=os.path.join(os.path.dirname(__file__), templates_dir),
        static_path=os.path.join(os.path.dirname(__file__), "static"),
        crypto_core=crypto_core,
        local_store=local_store,
        lookup_core=lookup_core,
        address_ctr=address_ctr,
        hooks_state=hooks_state,
        home=cfg["registration_domain"],
    )
    server = tornado.httpserver.HTTPServer(app, **{
        "ssl_options": cfg.get("ssl_options"),
        "xheaders": cfg.get("is_proxied")
    })
    server.listen(cfg["server_port"], cfg["server_addr"])

    if cfg.get("enable_dns_server", 0):
        server = dns_serve.server(crypto_core, local_store, cfg)
        server.start_thread()
        LOGGER.info("DNS server activated.")

    if "suid" in cfg:
        LOGGER.info("Descending...")
        if os.getuid() == 0:
            if ":" not in cfg["suid"]:
                user = cfg["suid"]
                group = None
            else:
                user, group = cfg["suid"].split(":", 1)
            uid = pwd.getpwnam(user).pw_uid
            if group:
                gid = grp.getgrnam(group).gr_gid
            else:
                gid = pwd.getpwnam(user).pw_gid
            os.setgid(gid)
            os.setuid(uid)
            LOGGER.info("Continuing.")
        else:
            LOGGER.info("suid key exists in config, but not running as root. "
                        "Exiting.")
            sys.exit()

    local_store.late_init()

    if "pid_file" in cfg:
        with open(cfg["pid_file"], "w") as pid:
            pid.write(str(os.getpid()))
    LOGGER.info("Notice: listening on {0}:{1}".format(
        cfg["server_addr"], cfg["server_port"]
    ))

    try:
        ioloop.start()
    finally:
        os.remove(cfg["pid_file"])

if __name__ == "__main__":
    main()
