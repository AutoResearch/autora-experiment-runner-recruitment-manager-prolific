import time
import random
import string
from typing import Any, List
import requests
import json

RETRIES = 20


def __save_get(url, headers):
    tries = 0
    while tries < RETRIES:
        tries += 1
        response = requests.get(url, headers=headers)
        if response.status_code < 400:
            return response.json()
        print(f'Warning in geting from prolific: {response.status_code}. Retry: {tries}/{RETRIES}')
        time.sleep(20)
    raise Exception(f'Error in geting from prolific: {response.status_code}')


def __save_post(url, headers, _json):
    tries = 0
    while tries < RETRIES:
        tries += 1
        response = requests.post(url, headers=headers, json=_json)
        if response.status_code < 400:
            return response.json()
        print(f'Warning in posting to prolific: {response.status_code}. Retry: {tries}/{RETRIES}')
        time.sleep(20)
    raise Exception(f'Error in posting to prolific: {response.status_code}')


def __save_patch(url, headers, _json):
    tries = 0
    while tries < RETRIES:
        tries += 1
        response = requests.patch(url, headers=headers, json=_json)
        if response.status_code < 400:
            return response.json()
        print(f'Warning in patching to prolific: {response.status_code}. Retry: {tries}/{RETRIES}')
        time.sleep(20)
    raise Exception(f'Error in patching to prolific: {response.status_code}')


def __get_request_results(url, headers):
    all_submissions = []
    while True:
        data = __save_get(url, headers)
        if "results" in data:
            all_submissions.extend(data["results"])

        next_page = data.get("next_page")
        if next_page:
            url = next_page
        else:
            break
    return all_submissions


def __get_request_results_id(url, headers):
    page = 1  # Start with the first page
    results_per_page = 50  # Specify the number of results per page

    # Concatenate all submissions
    all_submissions = []

    while True:
        data = __save_get(url, headers)
        url = data['_links']['next']['href']
        all_submissions.extend(data.get("results", []))
        if url is None:
            break

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
    return submissions


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
    submissions = _get_study_submissions(study_id, prolific_token)
    return [s['id'] for s in submissions
            if s['study_code'] == 'NOCODE'
            and s['status'] != 'RETURNED'
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
    print('setting up study on prolific')
    if eligibility_requirements is None:
        eligibility_requirements = []
    if exclude_studies is None:
        exclude_studies = []
    if exclude_studies == ["default"]:
        exclude_studies = [name]
    if eligibility_requirements == ["default"]:
        age_eligibility = EligibilityOptions.age(18, 55)
        nationality_eligibility = EligibilityOptions.nationality("United States", 1)
        vision_eligibility = EligibilityOptions.vision()
        language_eligibility = EligibilityOptions.first_language("English")
        eligibility_requirements = [
            age_eligibility,
            nationality_eligibility,
            vision_eligibility,
            language_eligibility,
        ]
    if _is_study_uncompleted(name, prolific_token):
        still_uncomplete = True
        for i in range(10):
            time.sleep(30)
            still_uncomplete = still_uncomplete and _is_study_uncompleted(name, prolific_token)
            if still_uncomplete:
                _approve_study_incompleted_submissions(name, prolific_token)
            still_uncomplete = still_uncomplete and _is_study_uncompleted(name, prolific_token)
            if not still_uncomplete:
                break
        if still_uncomplete:
            print('ERROR: There is a study with this name that is not completed. Can not proceed.')
            return
    previous_studies = _list_studies(prolific_token)
    excludes = [
        {"name": s["name"], "id": s["id"]}
        for s in previous_studies
        if s["name"] in exclude_studies
    ]
    if excludes is not []:
        eligibility_requirements += [EligibilityOptions.previous_studies(excludes)]
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

    _json = {
        'name': name,
        'description': description,
        'external_study_url': external_study_url,
        'estimated_completion_time': estimated_completion_time,
        'reward': reward,
        'prolific_id_option': prolific_id_option,
        'completion_code': completion_code,
        'completion_option': completion_option,
        'total_available_places': total_available_places,
        'eligibility_requirements': eligibility_requirements,
        'device_compatibility': device_compatibility,
        'peripheral_requirements': peripheral_requirements,
        'completion_code_action': "AUTOMATICALLY_APPROVE"
    }
    # packages function parameters into dictionary
    # _json = locals()

    # _json["completion_code_action"] = "AUTOMATICALLY_APPROVE"

    data = __save_post(
        "https://api.prolific.com/api/v1/studies/",
        headers={"Authorization": f"Token {prolific_token}"},
        _json=_json,
    )
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
    return _update_study_status(study_id, "PAUSE", prolific_token)


def start_study(study_id: str, prolific_token: str):
    """
    Starts/Resumes the study
    """
    return _update_study_status(study_id, "START", prolific_token)


def publish_study(study_id: str, prolific_token: str):
    """
    Publish the study
    """
    return _update_study_status(study_id, "PUBLISH", prolific_token)


def _get_submissions(study_id: str, prolific_token: str):
    study = __get_request_results_id(
        f"https://api.prolific.com/api/v1/studies/{study_id}/submissions/",
        {"Authorization": f"Token {prolific_token}"})
    return study


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
