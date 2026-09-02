"""
devices/hikvision_isapi.py

Hikvision ISAPI client — faithfully based on the original working
hikvision_isapi.py, faces.py, photo_sync.py and device_photos.py.
"""
import json
import logging
import requests
from requests.auth import HTTPDigestAuth
from datetime import datetime, timedelta
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)


class HikvisionISAPI:
    def __init__(self, ip, username, password, timeout=15, use_https=False, profile=None):
        self.ip       = ip
        self.username = username
        self.password = password
        self.timeout  = timeout
        self.verify   = False
        self.base = f"http://{ip}/ISAPI"
        self.auth = HTTPDigestAuth(username, password)
        self.auth_scheme = "digest"
        self.profile = {}
        if profile:
            self.apply_profile(profile)

    @classmethod
    def from_row(cls, row):
        """Build a client from a device DB row/dict, auto-loading its stored
        profile if present. Tolerates rows that lack the profile column."""
        def g(k, default=None):
            try:
                return row[k]
            except Exception:
                return default
        prof = g("profile")
        return cls(g("ip"), g("username"), g("password"), profile=prof)

    def apply_profile(self, profile):
        """Load a stored device profile (dict or JSON string) so calls use the
        per-device dialect detected at add-time."""
        import json as _json
        if isinstance(profile, str):
            try:
                profile = _json.loads(profile)
            except Exception:
                profile = {}
        self.profile = profile or {}
        sch = self.profile.get("auth_scheme")
        if sch in ("digest", "basic"):
            self.auth_scheme = sch

    def _fresh_auth(self):
        """
        Return a brand-new auth object using the device's detected scheme
        (digest by default). Some terminal models invalidate the digest nonce
        after a preceding read, causing the next WRITE to fail with userCheck
        401 even though credentials are valid. A fresh auth object does its own
        handshake and avoids the stale-nonce 401. If a profile selected
        basic-auth for this model, use that instead.
        """
        scheme = getattr(self, "auth_scheme", "digest")
        if scheme == "basic":
            from requests.auth import HTTPBasicAuth
            return HTTPBasicAuth(self.username, self.password)
        return HTTPDigestAuth(self.username, self.password)

    def _detect_auth_scheme(self) -> dict:
        """
        Probe which auth scheme this device accepts for a WRITE. Tries, in order:
          1. digest (fresh handshake)
          2. basic
        Uses a harmless write-shaped request (UserInfo/Search is a POST that
        requires auth but changes nothing) to test acceptance without creating
        data. Returns {"scheme": str|None, "detail": str}.
        """
        from requests.auth import HTTPBasicAuth
        url = f"{self.base}/AccessControl/UserInfo/Search?format=json"
        payload = {"UserInfoSearchCond": {"searchID": "probe",
                   "searchResultPosition": 0, "maxResults": 1}}
        attempts = [
            ("digest", HTTPDigestAuth(self.username, self.password)),
            ("basic",  HTTPBasicAuth(self.username, self.password)),
        ]
        for scheme, auth in attempts:
            try:
                r = requests.post(url, json=payload, auth=auth,
                                  timeout=self.timeout, verify=self.verify)
                if r.status_code < 400 and "401" not in (r.text or ""):
                    return {"scheme": scheme, "detail": f"{scheme} accepted (HTTP {r.status_code})"}
            except Exception as e:
                last = str(e)
        return {"scheme": None,
                "detail": "all auth schemes returned 401/Unauthorized — likely a device-side account/permission issue, not a code problem"}



    # ── Connectivity ──────────────────────────────────────────────────────────

    def ping(self) -> bool:
        try:
            r = requests.get(f"{self.base}/System/deviceInfo?format=json",
                             auth=self.auth, timeout=5, verify=self.verify)
            return r.status_code < 400
        except Exception:
            return False

    # ── Capability probe (read-only) ──────────────────────────────────────────

    def run_write_test(self, test_image: bytes = None) -> dict:
        """
        DESTRUCTIVE-but-self-cleaning write test for a throwaway device.

        Creates a temporary user (employeeNo from a high, unlikely-to-collide
        range), pushes a face, verifies, then deletes BOTH the face and the
        user. Cleanup runs in a finally block so a partial failure still tries
        to remove everything it created.

        Returns ordered {step: {"ok": bool, "detail": str}}.
        NOTE: caller must gate this behind an explicit user action.
        """
        TEST_EMP  = "999000001"          # high range, unlikely to clash
        TEST_NAME = "_PROBE_TEST"
        results = {}
        created_user = False
        uploaded_face = False

        def record(key, ok, detail=""):
            # Preserve None to mean "skipped"; otherwise coerce to bool
            results[key] = {"ok": (None if ok is None else bool(ok)),
                            "detail": str(detail)[:200]}

        # Refuse to clobber a real user that happens to share the test ID
        try:
            if TEST_EMP in self.list_users():
                record("precheck", False,
                       f"employeeNo {TEST_EMP} already exists — aborting to avoid touching real data")
                return results
            record("precheck", True, f"test id {TEST_EMP} is free")
        except Exception as e:
            record("precheck", False, f"could not list users: {e}")
            return results

        try:
            # Step 1: create user (exercises RightPlan fallback + JSON status)
            ok, msg = self.create_or_update_user(TEST_EMP, TEST_NAME)
            created_user = ok
            record("user_create", ok, msg[:160] if not ok else "created")

            # Step 2: confirm it shows up in the user list
            if created_user:
                import time as _t; _t.sleep(1.5)
                present = TEST_EMP in self.list_users()
                record("user_readback", present,
                       "found in UserInfo/Search" if present else "not found after create")

            # Step 3: face upload (only if we have an image and the user exists)
            if created_user and test_image:
                import time as _t; _t.sleep(2)
                fok, fmsg = self.upload_face(TEST_EMP, test_image, name=TEST_NAME)
                uploaded_face = fok
                record("face_upload", fok, fmsg[:160] if not fok else "uploaded")
                if fok:
                    import time as _t; _t.sleep(2)
                    face_ok = self.face_exists(TEST_EMP)
                    record("face_readback", face_ok,
                           "FDSearch confirms face" if face_ok
                           else "face not found after upload")
            elif created_user:
                record("face_upload", None, "skipped — no test image provided")

        finally:
            # Cleanup ALWAYS — face first, then user
            if uploaded_face or created_user:
                import time as _t; _t.sleep(1)
            if uploaded_face:
                record("face_cleanup", self.delete_face(TEST_EMP), "delete_face")
            if created_user:
                deleted = self.delete_user(TEST_EMP)
                # Verify it's actually gone
                gone = deleted
                try:
                    gone = TEST_EMP not in self.list_users()
                except Exception:
                    pass
                record("user_cleanup", gone,
                       "user removed" if gone else "WARNING: test user may remain — check device")

        return results

    def detect_profile(self, test_image: bytes = None) -> dict:
        """
        Characterise this device and return a profile dict that later calls use
        to pick the right ISAPI dialect per device. Runs auth detection and a
        set of read probes, then (using the throwaway _PROBE_TEST user) detects
        the write-dependent variants: whether SetUp wants RightPlan, and which
        delete verb works. Self-cleans any test user/face it creates.

        Profile keys:
          auth_scheme       : "digest" | "basic" | None
          rightplan         : True | False | None   (does SetUp need RightPlan?)
          delete_verb       : "PUT" | "DELETE" | None
          face_mode         : "multipart" | "faceurl" | None  (None = untested)
          searchid          : "uuid" (we standardise on a generated id)
          event_tz          : True   (times need a tz offset)
          face_status       : "numofface"  (use numOfFace from UserInfo/Search)
          detected_at       : ISO timestamp
          notes             : list[str]
        """
        import uuid as _uuid
        from datetime import datetime as _dt
        prof = {
            "auth_scheme": None, "rightplan": None, "delete_verb": None,
            "face_mode": None, "searchid": "uuid", "event_tz": True,
            "face_status": "numofface",
            "detected_at": _dt.now().isoformat(timespec="seconds"),
            "notes": [],
        }

        # 1. Auth scheme
        a = self._detect_auth_scheme()
        prof["auth_scheme"] = a["scheme"]
        prof["notes"].append(f"auth: {a['detail']}")
        if a["scheme"] is None:
            # No write auth works — stop. This is the 401/isActivated case.
            prof["notes"].append("Aborting write-dependent probes; fix the device account first.")
            return prof
        # Adopt detected scheme for the rest of this probe session
        self.auth_scheme = a["scheme"]

        TEST_EMP, TEST_NAME = "999000002", "_PROFILE_PROBE"

        # Guard: never touch a real user that happens to share the id
        try:
            if TEST_EMP in self.list_users():
                prof["notes"].append(f"{TEST_EMP} already exists; skipping write-probe.")
                return prof
        except Exception as e:
            prof["notes"].append(f"could not list users: {e}")
            return prof

        created = False
        try:
            # 2. RightPlan: try WITH first; if it fails on the RightPlan field,
            #    try WITHOUT. Record which one the device accepted.
            url = f"{self.base}/AccessControl/UserInfo/SetUp?format=json"

            def _user(with_rp):
                ui = {
                    "employeeNo": TEST_EMP, "name": TEST_NAME, "userType": "normal",
                    "Valid": {"enable": True,
                              "beginTime": _dt.now().strftime("%Y-%m-%dT%H:%M:%S"),
                              "endTime": "2037-12-31T23:59:59", "timeType": "local"},
                    "doorRight": "1",
                }
                if with_rp:
                    ui["RightPlan"] = [{"doorNo": 1, "planTemplateNo": "1"}]
                return {"UserInfo": ui}

            def _ok(resp):
                if resp.status_code >= 400:
                    return False
                try:
                    b = resp.json()
                    if str(b.get("subStatusCode", "")).lower() in ("ok", "") and \
                       b.get("statusCode") in (1, None):
                        return True
                    return False
                except Exception:
                    return resp.status_code < 300

            r = requests.put(url, json=_user(True), auth=self._fresh_auth(),
                             timeout=self.timeout, verify=self.verify)
            if _ok(r):
                prof["rightplan"] = True
                created = True
                prof["notes"].append("SetUp accepts RightPlan")
            else:
                r2 = requests.put(url, json=_user(False), auth=self._fresh_auth(),
                                  timeout=self.timeout, verify=self.verify)
                if _ok(r2):
                    prof["rightplan"] = False
                    created = True
                    prof["notes"].append("SetUp rejected RightPlan; works without it")
                else:
                    prof["notes"].append(
                        f"SetUp failed both with and without RightPlan: {r.text[:80]}")

            # 3. delete verb — only if we created a user to delete
            if created:
                import time as _t; _t.sleep(1)
                durl = f"{self.base}/AccessControl/UserInfo/Delete?format=json"
                dpayload = {"UserInfoDelCond": {"EmployeeNoList": [{"employeeNo": TEST_EMP}]}}
                rp = requests.put(durl, json=dpayload, auth=self._fresh_auth(),
                                  timeout=self.timeout, verify=self.verify)
                if rp.status_code < 400:
                    prof["delete_verb"] = "PUT"
                    created = False
                    prof["notes"].append("delete via PUT works")
                else:
                    rd = requests.delete(durl, json=dpayload, auth=self._fresh_auth(),
                                         timeout=self.timeout, verify=self.verify)
                    if rd.status_code < 400:
                        prof["delete_verb"] = "DELETE"
                        created = False
                        prof["notes"].append("delete via DELETE works")
                    else:
                        prof["notes"].append("WARNING: neither PUT nor DELETE removed test user")
        finally:
            # Guarantee cleanup if the user still exists
            if created:
                try:
                    self.delete_user(TEST_EMP)
                except Exception:
                    pass
                try:
                    if TEST_EMP in self.list_users():
                        prof["notes"].append(f"WARNING: test user {TEST_EMP} may remain — verify on device")
                except Exception:
                    pass

        return prof

    def probe_capabilities(self) -> dict:
        """
        Run READ-ONLY checks against the quirk points we've hit on these
        terminals, so a newly added device can be characterised before relying
        on it. Performs NO writes: no user create/update, no face upload/delete.

        Returns an ordered dict of {check_name: {"ok": bool, "detail": str}}.
        """
        import json as _json
        results = {}

        def record(key, ok, detail=""):
            results[key] = {"ok": bool(ok), "detail": str(detail)[:200]}

        # 1. Reachability + device info
        try:
            r = requests.get(f"{self.base}/System/deviceInfo?format=json",
                             auth=self.auth, timeout=self.timeout, verify=self.verify)
            if r.status_code < 400:
                model = ""
                try:
                    info = r.json().get("DeviceInfo", {})
                    model = f"{info.get('deviceName','')} fw {info.get('firmwareVersion','')}"
                except Exception:
                    pass
                record("reachable", True, model or "deviceInfo OK")
            else:
                record("reachable", False, f"HTTP {r.status_code}")
                return results  # nothing else will work
        except Exception as e:
            record("reachable", False, str(e))
            return results

        # 2. Device clock vs server clock (event-time bugs trace back here)
        try:
            r = requests.get(f"{self.base}/System/time?format=json",
                             auth=self.auth, timeout=self.timeout, verify=self.verify)
            if r.status_code < 400:
                t = r.json().get("Time", {})
                dev_time = t.get("localTime", "")
                from datetime import datetime as _dt
                detail = f"device={dev_time}"
                try:
                    dev = _dt.fromisoformat(dev_time[:19])
                    skew = abs((_dt.now() - dev).total_seconds())
                    detail = f"device={dev_time[:19]} skew={int(skew)}s"
                    record("clock_sync", skew < 120, detail)
                except Exception:
                    record("clock_sync", True, detail)
            else:
                record("clock_sync", False, f"HTTP {r.status_code}")
        except Exception as e:
            record("clock_sync", False, str(e))

        # 3. User list (UserInfo/Search) — paging + count
        try:
            users = self.list_users()
            record("user_search", True, f"{len(users)} users on device")
        except Exception as e:
            record("user_search", False, str(e))

        # 4. Event fetch (AcsEvent) — the searchID/timezone path. Read-only:
        #    a tiny 1-hour window today, just to confirm the endpoint answers.
        try:
            from datetime import datetime as _dt, timedelta as _td
            import os as _os
            tz = _os.environ.get("TZ_OFFSET", "-06:00")
            end = _dt.now()
            start = end - _td(hours=1)
            fmt = "%Y-%m-%dT%H:%M:%S"
            evs = self.fetch_events(start.strftime(fmt) + tz,
                                    end.strftime(fmt) + tz)
            record("event_fetch", True, f"endpoint OK ({len(evs)} events in last hour)")
        except Exception as e:
            record("event_fetch", False, str(e))

        # 5. Face library read (FDLib list) + FDSearch availability
        try:
            faces = self.list_faces_on_device()
            record("face_list", True, f"{len(faces)} faces in FDLib")
        except Exception as e:
            record("face_list", False, str(e))
        try:
            record("fdsearch_supported", self.supports_fdsearch(),
                   "FDSearch endpoint responds")
        except Exception as e:
            record("fdsearch_supported", False, str(e))

        return results

    # ── User management ───────────────────────────────────────────────────────

    def create_or_update_user(self, employee_no: str, name: str,
                               user_type: str = "normal"):
        """
        PUT /ISAPI/AccessControl/UserInfo/SetUp
        doorNo must be integer 1 (not string "1").
        """
        url = f"{self.base}/AccessControl/UserInfo/SetUp?format=json"

        def _build(with_rightplan: bool):
            ui = {
                "employeeNo": str(employee_no),
                "name":       name or str(employee_no),
                "userType":   user_type,
                "Valid": {
                    "enable":    True,
                    "beginTime": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    "endTime":   (datetime.now() + timedelta(days=365*5))
                                 .strftime("%Y-%m-%dT%H:%M:%S"),
                    "timeType":  "local",
                },
                "doorRight": "1",
            }
            if with_rightplan:
                ui["RightPlan"] = [{"doorNo": 1, "planTemplateNo": "1"}]
            return {"UserInfo": ui}

        def _succeeded(resp):
            """Device may return HTTP 200 with an error body. Confirm via JSON."""
            if resp.status_code >= 400:
                return False, resp.text
            try:
                body = resp.json()
            except Exception:
                # No JSON body — fall back to HTTP code (treat 2xx as ok)
                return resp.status_code < 300, resp.text
            sub = str(body.get("subStatusCode", "")).lower()
            code = body.get("statusCode")
            # statusCode 1 == OK on these devices; "ok" subStatus also OK
            if code in (1, None) or sub in ("ok", ""):
                return True, resp.text
            return False, resp.text

        try:
            # Use the profile's known RightPlan preference if we have one,
            # otherwise default to trying WITH RightPlan first.
            rp_pref = self.profile.get("rightplan")
            first_with_rp = (rp_pref is not False)  # True or None → try with first
            r = requests.put(url, json=_build(first_with_rp), auth=self._fresh_auth(),
                             timeout=self.timeout, verify=self.verify)
            ok, txt = _succeeded(r)
            # Some models invalidate the digest nonce after a preceding read,
            # returning userCheck 401 on the first write. Retry once with a new
            # handshake before giving up.
            if not ok and (r.status_code == 401 or "userCheck" in txt
                           or "Unauthorized" in txt):
                import time as _t; _t.sleep(0.5)
                log.info(f"[ISAPI] {employee_no}: 401 on write, retrying with fresh digest")
                r = requests.put(url, json=_build(first_with_rp), auth=self._fresh_auth(),
                                 timeout=self.timeout, verify=self.verify)
                ok, txt = _succeeded(r)
            # Fallback to the other RightPlan variant if the device rejected it
            if not ok and ("planTemplateNo" in txt or "badJsonContent" in txt
                           or "RightPlan" in txt):
                log.info(f"[ISAPI] {employee_no}: retrying SetUp with RightPlan={not first_with_rp}")
                r = requests.put(url, json=_build(not first_with_rp), auth=self._fresh_auth(),
                                 timeout=self.timeout, verify=self.verify)
                ok, txt = _succeeded(r)
            if not ok:
                log.warning(f"[ISAPI] create_user {employee_no} → {r.status_code}: {txt[:120]}")
            return ok, txt
        except Exception as e:
            return False, str(e)

    def list_users(self, page_size: int = 50) -> set:
        """Return set of employeeNo strings on the device."""
        import uuid
        url  = f"{self.base}/AccessControl/UserInfo/Search?format=json"
        search_id = str(uuid.uuid4())
        pos, out = 0, set()
        while True:
            payload = {"UserInfoSearchCond": {
                "searchID": search_id,
                "searchResultPosition": pos,
                "maxResults": page_size,
            }}
            try:
                r = requests.post(url, json=payload, auth=self.auth,
                                  timeout=self.timeout, verify=self.verify)
                if r.status_code >= 400:
                    break
                data  = r.json()
                root  = (data.get("UserInfoSearch") or
                         data.get("UserInfoSearchResult") or {})
                users = root.get("UserInfo", [])
                if isinstance(users, dict):
                    users = [users]
                if not users:
                    break
                for u in users:
                    emp = u.get("employeeNo") or u.get("employeeNoString")
                    if emp:
                        out.add(str(emp).strip())
                if root.get("responseStatusStrg") != "MORE":
                    break
                pos += len(users)
            except Exception as e:
                log.debug(f"[ISAPI] list_users error: {e}")
                break
        return out

    # ── Face upload ───────────────────────────────────────────────────────────

    def upload_face(self, employee_no: str, image_bytes: bytes,
                    fdid: str = "1", name: str = None):
        """
        Upload face via multipart PUT to /ISAPI/Intelligent/FDLib/FDSetUp
        Uses employeeNo as FPID so each employee gets a unique face slot.
        Employee must already exist on device before calling this.
        """
        import json as _json

        if not image_bytes or len(image_bytes) < 100:
            return False, "No image data"

        metadata = _json.dumps({
            "faceLibType": "blackFD",
            "FDID": fdid,
            "FPID": str(employee_no),
            "name": name or str(employee_no),
            "employeeNo": str(employee_no),
        })

        url = f"{self.base}/Intelligent/FDLib/FDSetUp?format=json"
        try:
            r = requests.put(
                url,
                auth=self._fresh_auth(),
                files={
                    "FaceDataRecord": (None, metadata, "application/json"),
                    "FaceImage": (f"{employee_no}.jpg", image_bytes, "image/jpeg"),
                },
                timeout=30,
                verify=self.verify
            )
            ok = r.status_code < 400
            if not ok:
                log.warning(f"[ISAPI] upload_face {employee_no} -> {r.status_code}: {r.text[:150]}")
            return ok, r.text
        except Exception as e:
            return False, str(e)

    def delete_face(self, employee_no: str, fdid: str = "1") -> bool:
        url = f"{self.base}/Intelligent/FDLib/FDSetUp?format=json"
        payload = {"FDID": str(fdid), "FPID": str(employee_no),
                   "faceLibType": "blackFD"}
        try:
            r = requests.delete(url, json=payload, auth=self._fresh_auth(),
                                timeout=self.timeout, verify=self.verify)
            return r.status_code < 400
        except Exception:
            return False

    def delete_user(self, employee_no: str) -> bool:
        """Delete a user via PUT to UserInfo/Delete (these devices reject HTTP DELETE)."""
        url = f"{self.base}/AccessControl/UserInfo/Delete?format=json"
        payload = {"UserInfoDelCond": {"EmployeeNoList": [{"employeeNo": str(employee_no)}]}}
        try:
            r = requests.put(url, json=payload, auth=self._fresh_auth(),
                             timeout=self.timeout, verify=self.verify)
            return r.status_code < 400
        except Exception:
            return False

    def face_exists(self, employee_no: str, fdid: str = "1") -> bool:
        url = f"{self.base}/Intelligent/FDLib/FDSearch?format=json"
        payload = {"FDSearchCond": {
            "searchID": "1", "searchResultPosition": 0, "maxResults": 1,
            "faceLibType": "blackFD", "FDID": str(fdid), "FPID": str(employee_no),
        }}
        try:
            r = requests.post(url, json=payload, auth=self.auth,
                              timeout=self.timeout, verify=self.verify)
            if r.status_code >= 400:
                return False
            root = r.json().get("FDSearch") or r.json()
            return int(root.get("numOfMatches") or 0) > 0
        except Exception:
            return False

    # ── Face download ─────────────────────────────────────────────────────────

    def supports_fdsearch(self) -> bool:
        """Check if device supports FDSearch (face download)."""
        try:
            r = requests.get(
                f"{self.base}/Intelligent/FDLib/capabilities?format=json",
                auth=self.auth, timeout=5, verify=self.verify
            )
            if r.status_code == 200:
                data = r.json()
                return bool(data.get("isSuportFDSearch", False))
        except Exception:
            pass
        return False

    def download_face(self, employee_no: str, fdid: str = "1"):
        """
        Download enrolled face image from device using FPID=employeeNo.
        Searches FDLib by FPID to get the face URL, then downloads it.
        """
        try:
            r = requests.post(
                f"{self.base}/Intelligent/FDLib/FDSearch?format=json",
                json={
                    "searchResultPosition": 0,
                    "maxResults": 1,
                    "faceLibType": "blackFD",
                    "FDID": fdid,
                    "FPID": str(employee_no),
                },
                auth=self.auth, timeout=15, verify=self.verify
            )
            if r.status_code == 200:
                matches = r.json().get("MatchList", [])
                if matches:
                    face_url = matches[0].get("faceURL", "")
                    if face_url:
                        if not face_url.startswith("http"):
                            face_url = f"http://{self.ip}{face_url}"
                        r2 = requests.get(face_url, auth=self.auth,
                                          timeout=20, verify=self.verify)
                        if r2.status_code == 200 and len(r2.content) > 100:
                            if r2.content[:2] == b'\xff\xd8' or r2.content[:4] == b'\x89PNG':
                                log.info(f"[ISAPI] download_face {employee_no} OK ({len(r2.content)}B)")
                                return r2.content
        except Exception as e:
            log.debug(f"[ISAPI] download_face {employee_no} error: {e}")

        log.debug(f"[ISAPI] download_face {employee_no} — failed")
        return None

    def list_faces_on_device(self, fdid: str = "1", page_size: int = 50) -> set:
        """Return set of employee_no strings that have a face on this device."""
        url  = f"{self.base}/Intelligent/FDLib/FDSearch?format=json"
        pos, out = 0, set()
        while True:
            payload = {"FDSearchCond": {
                "searchID": "1", "searchResultPosition": pos,
                "maxResults": page_size, "faceLibType": "blackFD", "FDID": str(fdid),
            }}
            try:
                r = requests.post(url, json=payload, auth=self.auth,
                                  timeout=self.timeout, verify=self.verify)
                if r.status_code >= 400:
                    break
                root    = r.json().get("FDSearch") or r.json()
                matches = root.get("MatchList") or root.get("FaceDataRecord") or []
                if isinstance(matches, dict):
                    matches = [matches]
                if not matches:
                    break
                for m in matches:
                    fpid = m.get("FPID") or m.get("employeeNo")
                    if fpid:
                        out.add(str(fpid).strip())
                if root.get("responseStatusStrg") != "MORE":
                    break
                pos += len(matches)
            except Exception:
                break
        return out

    # ── Event fetching ────────────────────────────────────────────────────────

    def fetch_events(self, start_iso: str, end_iso: str,
                      page_size: int = 30) -> list:
        """
        Fetch access events with full pagination.
        Uses /ISAPI/AccessControl/AcsEvent with major=5, minor=75.
        """
        import os as _os
        tz = _os.environ.get("TZ_OFFSET", "-06:00")
        url    = f"{self.base}/AccessControl/AcsEvent?format=json"
        all_events = []
        position   = 0

        while True:
            payload = {
                "AcsEventCond": {
                    "searchID":             "0",
                    "searchResultPosition": position,
                    "maxResults":           page_size,
                    "major":                5,
                    "minor":                75,
                    # Device requires TZ offset; don't double-append if caller already included it
                    "startTime":            start_iso if len(start_iso) > 19 else f"{start_iso}{tz}",
                    "endTime":              end_iso   if len(end_iso)   > 19 else f"{end_iso}{tz}",
                }
            }
            try:
                r = requests.post(url, json=payload, auth=self.auth,
                                  timeout=30, verify=self.verify)
                if r.status_code >= 400:
                    break

                data   = r.json()
                root   = data.get("AcsEvent") or {}
                events = root.get("InfoList", [])
                if isinstance(events, dict):
                    events = [events]
                if not events:
                    break

                all_events.extend(events)
                if root.get("responseStatusStrg", "") != "MORE":
                    break
                position += len(events)

            except Exception as e:
                log.debug(f"[ISAPI] AcsEvent pagination error: {e}")
                break

        if all_events:
            log.info(f"[ISAPI] fetch_events total: {len(all_events)} events")
            return all_events

        # Fallback: EventSearch (older firmware)
        try:
            url2 = f"{self.base}/Event/Search?format=json"
            payload2 = {"EventSearchCond": {
                "searchID": "1", "searchResultPosition": 0,
                "maxResults": page_size,
                "startTime": start_iso, "endTime": end_iso,
            }}
            r2 = requests.post(url2, json=payload2, auth=self.auth,
                               timeout=30, verify=self.verify)
            if r2.status_code < 400:
                root2   = r2.json().get("EventSearch") or {}
                events2 = root2.get("InfoList", [])
                if isinstance(events2, dict):
                    events2 = [events2]
                return events2 or []
        except Exception as e:
            log.debug(f"[ISAPI] EventSearch error: {e}")

        return []
