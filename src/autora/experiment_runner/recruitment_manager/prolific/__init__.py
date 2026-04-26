import time
import random
import string
from datetime import datetime
from typing import Any, List
import requests
import json

RETRIES = 5
REQUEST_TIMEOUT_SECONDS = (5, 20)
"""``(connect_timeout, read_timeout)`` in seconds. Tuple form is necessary
on macOS / corporate networks where a single ``timeout=20`` does not
reliably bound the underlying ``socket.readinto`` (urllib3 will sit in a
blocking read forever on a stalled keep-alive connection). The tuple
sets both an OS connect timeout and a per-byte read timeout.
"""

RETRY_SLEEP_SECONDS = 5
"""Backoff between retries. Worst-case stall per call is roughly
``RETRIES * (REQUEST_TIMEOUT_SECONDS[1] + RETRY_SLEEP_SECONDS)`` —
with the defaults above ~ 5 * (20 + 5) = 125 s, vs. the previous
~13 minutes (20 * 40 s)."""

PAGINATION_MAX_PAGES = 200
"""Hard ceiling for ``__get_request_results_id`` so a malformed
``_links.next.href`` (Prolific occasionally returns the same URL again
or a non-null href on the last page) cannot trap us in an infinite
fetch loop."""

DEFAULT_COUNTRY_FILTER_ID = "current-country-of-residence"
DEFAULT_COUNTRY_US_VALUE = "1"


def _log(message: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[prolific_runner {ts}] {message}", flush=True)


def __save_get(url, headers):
    tries = 0
    response = None
    while tries < RETRIES:
        tries += 1
        _log(f"GET {url} (try {tries}/{RETRIES})")
        try:
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            _log(
                f"GET {url} failed ({type(exc).__name__}: {exc}); "
                f"retry in {RETRY_SLEEP_SECONDS}s"
            )
            time.sleep(RETRY_SLEEP_SECONDS)
            continue
        if response.status_code < 400:
            return response.json()
        _log(
            f"GET {url} -> {response.status_code}; retry in {RETRY_SLEEP_SECONDS}s"
        )
        time.sleep(RETRY_SLEEP_SECONDS)
    status = response.status_code if response is not None else "no response"
    raise Exception(f'Error in getting from prolific: {status} (after {RETRIES} tries) {url}')


def __save_post(url, headers, _json):
    tries = 0
    response = None
    while tries < RETRIES:
        tries += 1
        _log(f"POST {url} (try {tries}/{RETRIES})")
        try:
            response = requests.post(
                url,
                headers=headers,
                json=_json,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            _log(
                f"POST {url} failed ({type(exc).__name__}: {exc}); "
                f"retry in {RETRY_SLEEP_SECONDS}s"
            )
            time.sleep(RETRY_SLEEP_SECONDS)
            continue
        if response.status_code < 400:
            return response.json()
        detail = (response.text or "")[:500]
        _log(
            f"POST {url} -> {response.status_code} ({detail}); "
            f"retry in {RETRY_SLEEP_SECONDS}s"
        )
        time.sleep(RETRY_SLEEP_SECONDS)
    status = response.status_code if response is not None else "no response"
    raise Exception(f"Error in posting to prolific: {status} (after {RETRIES} tries) {url}")


def __save_patch(url, headers, _json):
    tries = 0
    response = None
    while tries < RETRIES:
        tries += 1
        _log(f"PATCH {url} (try {tries}/{RETRIES})")
        try:
            response = requests.patch(
                url,
                headers=headers,
                json=_json,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            _log(
                f"PATCH {url} failed ({type(exc).__name__}: {exc}); "
                f"retry in {RETRY_SLEEP_SECONDS}s"
            )
            time.sleep(RETRY_SLEEP_SECONDS)
            continue
        if response.status_code < 400:
            return response.json()
        _log(
            f"PATCH {url} -> {response.status_code}; retry in {RETRY_SLEEP_SECONDS}s"
        )
        time.sleep(RETRY_SLEEP_SECONDS)
    status = response.status_code if response is not None else "no response"
    raise Exception(f'Error in patching to prolific: {status} (after {RETRIES} tries) {url}')


def __get_request_results(url, headers):
    all_submissions = []
    pages = 0
    while True:
        pages += 1
        if pages > PAGINATION_MAX_PAGES:
            raise Exception(
                f"Pagination ceiling reached ({PAGINATION_MAX_PAGES} pages) "
                f"for {url}; suspect malformed `next_page` from Prolific."
            )
        data = __save_get(url, headers)
        if "results" in data:
            all_submissions.extend(data["results"])

        next_page = data.get("next_page")
        if not next_page or next_page == url:
            break
        url = next_page
    return all_submissions


def __get_request_results_id(url, headers):
    """Walk Prolific's HAL-style ``_links.next.href`` pagination.

    Prolific's submissions endpoint occasionally returns a non-null
    ``_links.next.href`` even on the final page (or returns the same href
    as the page we just fetched), which makes the upstream
    "while True: url = data['_links']['next']['href']" loop run forever
    in a blocking ``socket.readinto`` once the response stalls. We:

    * accept a missing ``_links`` / ``next`` / ``href`` (treat as terminal),
    * treat a non-string href (None, 0, etc.) as terminal,
    * break when the next href equals the current url (Prolific bug),
    * cap the loop at ``PAGINATION_MAX_PAGES`` so we error out loudly
      instead of hanging silently.
    """
    all_submissions: list = []
    pages = 0
    seen_urls: set = set()
    while True:
        pages += 1
        if pages > PAGINATION_MAX_PAGES:
            raise Exception(
                f"Pagination ceiling reached ({PAGINATION_MAX_PAGES} pages) "
                f"for {url}; suspect malformed `_links.next.href` from Prolific."
            )
        if url in seen_urls:
            _log(
                f"Pagination cycle detected at {url}; breaking to avoid "
                "infinite fetch."
            )
            break
        seen_urls.add(url)
        data = __save_get(url, headers)
        all_submissions.extend(data.get("results", []))
        next_url = (
            (data.get("_links") or {}).get("next", {}).get("href")
            if isinstance(data, dict) else None
        )
        if not isinstance(next_url, str) or not next_url or next_url == url:
            break
        url = next_url

    return all_submissions


def _list_studies(prolific_token: str):
    """
    Returns list of all studies on Prolific account.
    """
    studies = __get_request_results_id(
        "https://api.prolific.com/api/v1/studies/",
        {"Authorization": f"Token {prolific_token}"},
    )
    return studies


def _studies_from_name(study_name: str, prolific_token: str):
    """
    Returns the ids and status of studies with a given the name.
    """
    lst = _list_studies(prolific_token)
    return [{'id': s['id'], 'status': s['status']} for s in lst if s['name'] == study_name]


def _is_study_uncompleted(study_name: str, prolific_token: str):
    """
    Returns true if there is alread a study with the name that is not completed
    """
    lst = _studies_from_name(study_name, prolific_token)
    incomplete_lst = [s for s in lst if s['status'] != 'COMPLETED']
    return len(incomplete_lst) > 0


def _approve_study_incompleted_submissions(study_name: str, prolific_token: str):
    """
    Returns a list of incompleted submissions
    """
    lst = _studies_from_name(study_name, prolific_token)
    incomplete_lst = [s for s in lst if s['status'] != 'COMPLETED']
    submissions = []
    for s in incomplete_lst:
        submissions = __get_request_results(
            f'https://api.prolific.com/api/v1/submissions/?study={s["id"]}',
            {"Authorization": f"Token {prolific_token}"},
        )
        for sub in submissions:
            if sub['is_complete'] and sub['status'] == 'AWAITING REVIEW':
                __save_post(
                    f'https://api.prolific.com/api/v1/submissions/{sub["id"]}/transition/',
                    headers={"Authorization": f"Token {prolific_token}"},
                    _json={"action": "APPROVE"}
                )


def _get_study_submissions(study_id: str, prolific_token: str) -> dict:
    study = _retrieve_study(study_id, prolific_token)
    submissions = __get_request_results_id(
        study['_links']['related']['href'],
        {"Authorization": f"Token {prolific_token}"},
    )
    return _dedup_submissions(submissions)


def _get_submissions_type(study_id: str, prolific_token: str, type: str) -> list:
    submissions = _get_study_submissions(study_id, prolific_token)
    return [s['participant_id'] for s in submissions if s['status'] == type]


def _get_submissions_returned(study_id: str, prolific_token: str):
    return _get_submissions_type(study_id, prolific_token, 'RETURNED')


def _get_submissions_timed_out(study_id: str, prolific_token: str):
    return _get_submissions_type(study_id, prolific_token, 'TIMED-OUT')


def get_submissions_incompleted(study_id: str, prolific_token: str):
    return _get_submissions_returned(study_id, prolific_token) + \
        _get_submissions_type(study_id, prolific_token, 'TIMED-OUT')


def _get_submissions_no_code_not_returned(study_id: str, prolific_token: str):
    """Submissions a researcher needs to triage manually.

    A "no-code" submission is one Prolific recorded as ``study_code ==
    'NOCODE'`` (the participant never hit the completion-code redirect).
    The previous filter was too loose — it accepted any submission that
    was not yet RETURNED, which includes ACTIVE / RESERVED participants
    who simply have not entered a code yet because they're still doing
    the task. Calling ``approve_all_no_code`` or ``request_return_all``
    on those would either auto-approve a still-running participant
    (firing the "study finished" check in firebase_prolific and ending
    the round prematurely) or yank them out mid-task.

    The fix gates on ``is_complete`` *and* ``status == 'AWAITING REVIEW'``
    — together those guarantee Prolific has actually received a
    submission for this participant, and only the missing completion
    code stands between them and review.
    """
    submissions = _get_study_submissions(study_id, prolific_token)
    return [s['id'] for s in submissions
            if s['study_code'] == 'NOCODE'
            and s['is_complete']
            and s['status'] == 'AWAITING REVIEW'
            and not s['return_requested']]


def _request_return(id: str, prolific_token: str):
    print('request return')
    data = {"request_return_reasons": ["No completion code.", "Did not finish study."]}
    __save_post(
        f"https://api.prolific.com/api/v1/submissions/{id}/request-return/",
        headers={"Authorization": f"Token {prolific_token}"},
        _json=data,
    )


def _approve(id: str, prolific_token: str):
    __save_post(
        f'https://api.prolific.com/api/v1/submissions/{id}/transition/',
        headers={"Authorization": f"Token {prolific_token}"},
        _json={"action": "APPROVE"},
    )


def request_return_all(study_id: str, prolific_token: str):
    submissions = _get_submissions_no_code_not_returned(study_id, prolific_token)
    for id in submissions:
        _request_return(id, prolific_token)


def approve_all_no_code(study_id: str, prolific_token: str):
    submissions = _get_submissions_no_code_not_returned(study_id, prolific_token)
    for id in submissions:
        _approve(id, prolific_token)


def _update_study(study_id: str, prolific_token: str, **kwargs) -> bool:
    """
    Updates the parameters of a given study.
    If a study is already published, only internal_name
    and total_available_places can be updated.
    """
    tries = 0
    while tries < RETRIES:
        tries += 1
        response = requests.patch(
            f"https://api.prolific.com/api/v1/studies/{study_id}/",
            headers={"Authorization": f"Token {prolific_token}"},
            json=kwargs)
        if response.status_code < 400:
            return response.json()
        print(f'Warning in patching to prolific: {response.status_code}. Retry: {tries}/{RETRIES}')
        time.sleep(20)
    raise Exception(f'Error in patching to prolific: {response.status_code}')


def _retrieve_study(study_id: str, prolific_token: str):
    """
    Retrieves information about study given its ID.
    """
    return __save_get(
        f"https://api.prolific.com/api/v1/studies/{study_id}/",
        headers={"Authorization": f"Token {prolific_token}"},
    )


def check_prolific_status(study_id: str, prolific_token: str) -> dict:
    """
    Check the status of a study
    Args:
        study_id: id of the study
        prolific_token: a prolific api token

    Returns:
        Status of the study (Paused, Started, Finished ...)
    """
    study = _retrieve_study(study_id, prolific_token)
    keys_to_include = [
        "total_available_places",
        "places_taken",
        "status",
        "number_of_submissions",
    ]
    res = dict((key, value) for key, value in study.items() if key in keys_to_include)
    s_ids_awaiting_review = _get_submissions_by_status(study_id, prolific_token, 'AWAITING REVIEW')
    s_ids_approved = _get_submissions_by_status(study_id, prolific_token, 'APPROVED')
    res['number_of_submissions_finished'] = len(s_ids_approved) + len(s_ids_awaiting_review)
    return res


def _append_url_variable(url, variable):
    """
    appends an url variable if not already in url
    """
    if variable not in url:
        if '?' in url:
            url += f'&{variable}'
        else:
            url += f'?{variable}'
    return url


def setup_study(
        name: str,
        description: str,
        external_study_url: str,
        estimated_completion_time: int,
        prolific_token: str,
        exclude_studies: List[str] = ["default"],
        reward: int = 0,
        prolific_id_option: str = "url_parameters",
        completion_code: str = "",
        completion_option: str = "url",
        total_available_places: int = 1,
        eligibility_requirements: List[str] = ["default"],
        device_compatibility: List[str] = ["desktop"],
        peripheral_requirements=None,
        temp_file='',
        check_prev=True,

) -> Any:
    """
    Allows for a study to be drafted given the following parameters.

    Args:
        name (str): Name that will be displayed on prolific
        description (str): Description of study for participants
        external_study_url (str): URL to experiment website
        estimated_completion_time (int): How long the study takes
        prolific_token: (str): The Api token from your prolific-account
            (https://app.prolific.com under settings)
        exclude_studies (list): Exclude participants that participated in previous studies
            (default is studies with the same name)
        prolific_id_option (ProlificIdOptions): Method of collecting subject ID
        completion_code (str): Code subject uses to mark experiment completion
        completion_option (CompletionOptions): Method of signifying participation
        total_available_places (int): Participant limit
        reward (int): Amount of payment for completion
        eligibility_requirements (list, optional): Allows various options to filter participants.
            Defaults to [] (no requirements).
        device_compatibility (list[DeviceOptions], optional): Allows selecting required devices.
            Defaults to [] (any device).
        peripheral_requirements (list[PeripheralOptions], optional):
            Allows specifying additional requirements. Defaults to [] (no other requirements).
        temp_file (str, optional): File to save the study_id and max_allowed_time for restarting later

    Returns:
        dictionary: A dictionary with the id and maximum allowed time for the study (or False if something went wrong)
    """
    _log("Setting up study on Prolific")
    use_default_eligibility = eligibility_requirements == ["default"]
    if eligibility_requirements is None:
        eligibility_requirements = []
    if exclude_studies is None:
        exclude_studies = []
    if exclude_studies == ["default"]:
        exclude_studies = [name]
    if check_prev:
        _log("Checking for existing uncompleted studies with same name")
        if _is_study_uncompleted(name, prolific_token):
            still_uncomplete = True
            for i in range(10):
                _log(
                    f"Waiting for previous '{name}' study to complete/close ({i + 1}/10)"
                )
                time.sleep(30)
                still_uncomplete = still_uncomplete and _is_study_uncompleted(name, prolific_token)
                if still_uncomplete:
                    _approve_study_incompleted_submissions(name, prolific_token)
                still_uncomplete = still_uncomplete and _is_study_uncompleted(name, prolific_token)
                if not still_uncomplete:
                    break
            if still_uncomplete:
                _log('ERROR: There is a study with this name that is not completed. Can not proceed.')
                return
        previous_studies = _list_studies(prolific_token)
        excludes = [
            {"name": s["name"], "id": s["id"]}
            for s in previous_studies
            if s["name"] in exclude_studies
        ]
    else:
        excludes = exclude_studies
    if device_compatibility is None:
        device_compatibility = []
    if peripheral_requirements is None:
        peripheral_requirements = []
    if completion_code == "":
        completion_code = "".join(
            random.choices(string.ascii_letters + string.digits, k=6)
        )
    if reward == 0:
        reward = round(20 * estimated_completion_time)  # 12$ per hour / 20¢ per minute

    external_study_url = _append_url_variable(external_study_url, 'PROLIFIC_PID={{%PROLIFIC_PID%}}')

    # Prolific API v1 expects `filters` and `completion_codes` (not legacy eligibility / completion fields).
    filters: list[dict[str, Any]] = []
    if use_default_eligibility:
        filters.append(
            {"filter_id": "age", "selected_range": {"lower": 18, "upper": 55}}
        )
        filters.append(
            {
                "filter_id": DEFAULT_COUNTRY_FILTER_ID,
                "selected_values": [DEFAULT_COUNTRY_US_VALUE],
            }
        )
    elif eligibility_requirements:
        print(
            "Warning: Custom eligibility_requirements use a deprecated schema; "
            "applying defaults (age 18-55, US residence) only. "
            "Extend `setup_study` to pass modern `filters` if needed."
        )
        filters.append(
            {"filter_id": "age", "selected_range": {"lower": 18, "upper": 55}}
        )
        filters.append(
            {
                "filter_id": DEFAULT_COUNTRY_FILTER_ID,
                "selected_values": [DEFAULT_COUNTRY_US_VALUE],
            }
        )

    blocklist_ids: list[str] = []
    if excludes:
        for ex in excludes:
            if isinstance(ex, dict) and "id" in ex:
                blocklist_ids.append(str(ex["id"]))
            elif isinstance(ex, str):
                blocklist_ids.append(ex)
    if blocklist_ids:
        filters.append(
            {
                "filter_id": "previous_studies_blocklist",
                "selected_values": blocklist_ids,
            }
        )

    _json = {
        "name": name,
        "description": description,
        "external_study_url": external_study_url,
        "estimated_completion_time": estimated_completion_time,
        "reward": reward,
        "prolific_id_option": prolific_id_option,
        "total_available_places": total_available_places,
        "device_compatibility": device_compatibility,
        "peripheral_requirements": peripheral_requirements,
        "filters": filters,
        "completion_codes": [
            {
                "code": completion_code,
                "code_type": "COMPLETED",
                "actions": [{"action": "AUTOMATICALLY_APPROVE"}],
            }
        ],
    }

    data = __save_post(
        "https://api.prolific.com/api/v1/studies/",
        headers={"Authorization": f"Token {prolific_token}"},
        _json=_json,
    )
    _log("Prolific study draft created")
    keys_to_include = ["id", "maximum_allowed_time"]
    study_dict = dict(
        (key, value) for key, value in data.items() if key in keys_to_include
    )

    # save to temp_file
    if temp_file != '':
        if not str.endswith(temp_file, '.json'):
            raise ValueError(f"Error: File '{temp_file}' is not in the correct JSON format.")
        with open(temp_file, 'w') as file:
            json.dump(study_dict, file)
    return study_dict


def _update_study_status(study_id: str, action: str, prolific_token: str):
    """
    Performs action on specified study. Default action is to publish
    the study.
    """
    return __save_post(
        f"https://api.prolific.com/api/v1/studies/{study_id}/transition/",
        headers={"Authorization": f"Token {prolific_token}"},
        _json={"action": action},
    )


def pause_study(study_id: str, prolific_token: str):
    """
    Pauses the study
    """
    _log(f"Sending PAUSE transition for study {study_id}")
    return _update_study_status(study_id, "PAUSE", prolific_token)


def stop_study(study_id: str, prolific_token: str):
    """
    Pauses the study
    """
    _log(f"Sending STOP transition for study {study_id}")
    return _update_study_status(study_id, "STOP", prolific_token)


def start_study(study_id: str, prolific_token: str):
    """
    Starts/Resumes the study
    """
    _log(f"Sending START transition for study {study_id}")
    return _update_study_status(study_id, "START", prolific_token)


def publish_study(study_id: str, prolific_token: str):
    """
    Publish the study
    """
    _log(f"Sending PUBLISH transition for study {study_id}")
    return _update_study_status(study_id, "PUBLISH", prolific_token)


def _get_submissions(study_id: str, prolific_token: str):
    study = __get_request_results_id(
        f"https://api.prolific.com/api/v1/studies/{study_id}/submissions/",
        {"Authorization": f"Token {prolific_token}"})
    return _dedup_submissions(study)


def _dedup_submissions(submissions):
    """Collapse duplicate submission rows by ``id``, keep first occurrence.

    Defensive: Prolific's HAL pagination occasionally re-emits the same
    submission across page boundaries (we already break the page-loop
    when we see a duplicate ``next.href``, but the *contents* of two
    distinct pages can still overlap). Without this, ``check_prolific_status``
    can over-count ``number_of_submissions_finished`` (= APPROVED +
    AWAITING REVIEW) and trip the
    ``number_of_submissions_finished >= total_available_places`` early
    termination in ``firebase_prolific`` while real participants are
    still active.
    """
    seen: set = set()
    out: list = []
    for s in submissions or []:
        sid = s.get("id") if isinstance(s, dict) else None
        if sid is None or sid in seen:
            if sid is not None:
                continue
            out.append(s)  # malformed row, keep as-is so callers can see it
            continue
        seen.add(sid)
        out.append(s)
    return out


def _get_participants_by_status(study_id: str, prolific_token: str, status: str):
    results = _get_submissions(study_id, prolific_token)
    return [d['participant_id'] for d in results if d["status"] == status]


def _get_submissions_by_status(study_id: str, prolific_token: str, status: str):
    results = _get_submissions(study_id, prolific_token)
    return [d['id'] for d in results if d["status"] == status]


def get_participants_awaiting_review(study_id: str, prolific_token: str):
    return _get_participants_by_status(study_id, prolific_token, 'AWAITING REVIEW')

def get_submissions_awaiting_review(study_id: str, prolific_token: str):
    return _get_submissions_by_status(study_id, prolific_token, 'AWAITING REVIEW')


def get_participants_returned(study_id: str, prolific_token: str):
    return _get_participants_by_status(study_id, prolific_token, 'RETURNED')


def get_participants_timed_out(study_id: str, prolific_token: str):
    return _get_participants_by_status(study_id, prolific_token, 'TIMED OUT')


def approve_all(study_id: str, prolific_token: str):
    submissions = get_submissions_awaiting_review(study_id, prolific_token)
    for id in submissions:
        _approve(id, prolific_token)


class EligibilityOptions:
    @staticmethod
    def age(minimum: int, maximum: int):
        return {
            "_cls": "web.eligibility.models.AgeRangeEligibilityRequirement",
            "attributes": [
                {
                    "type": "min",
                    "max": 100,
                    "min": 18,
                    "value": minimum,
                    "name": "min_age",
                    "label": "Minimum Age",
                    "default_value": None,
                    "unit": "",
                    "step": 1,
                },
                {
                    "type": "max",
                    "max": 100,
                    "min": 18,
                    "value": maximum,
                    "name": "max_age",
                    "label": "Maximum Age",
                    "default_value": None,
                    "unit": "",
                    "step": 1,
                },
            ],
            "query": {"id": "54ac6ea9fdf99b2204feb893"},
        }

    @staticmethod
    def nationality(nationality: str, index: int):
        return {
            "_cls": "web.eligibility.models.SelectAnswerEligibilityRequirement",
            "attributes": [
                {
                    "label": nationality,
                    "name": nationality,
                    "value": True,
                    "index": index,
                },
            ],
            "query": {"id": "54bef0fafdf99b15608c504e"},
        }

    @staticmethod
    def vision():
        return {
            "_cls": "web.eligibility.models.SelectAnswerEligibilityRequirement",
            "attributes": [{"label": "Yes", "name": "Yes", "value": True, "index": 0}],
            "query": {"id": "57a0c4d2717b34954e81b919"},
        }

    @staticmethod
    def first_language(language: str):
        return {
            "_cls": "web.eligibility.models.SelectAnswerEligibilityRequirement",
            "attributes": [
                {"label": language, "name": language, "value": True, "index": 18}
            ],
            "query": {"id": "54ac6ea9fdf99b2204feb899"},
        }

    @staticmethod
    def previous_studies(studies):
        return {
            "_cls": "web.eligibility.models.PreviousStudiesEligibilityRequirement",
            "attributes": [
                {
                    "label": study["name"],
                    "value": True,
                    "id": study["id"],
                }
                for study in studies
            ],
            "query": {"id": ""},
        }
