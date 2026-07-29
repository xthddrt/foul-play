import logging
import os

import constants
from config import FoulPlayConfig
from data import all_move_json, pokedex
from data.pkmn_sets import make_user_set_key, record_user_set_observations
from fp.helpers import normalize_name, random_battles_evs
from teams.load_team import TEAM_DIR

logger = logging.getLogger(__name__)


def move_display_name(move_id):
    try:
        return all_move_json[move_id][constants.NAME]
    except KeyError:
        return move_id


def ability_display_name(species, ability_id):
    try:
        abilities = pokedex[normalize_name(species)][constants.ABILITIES]
    except KeyError:
        return ability_id
    for ability in abilities.values():
        if normalize_name(ability) == ability_id:
            return ability
    return ability_id


def pokemon_export_from_request_dict(pkmn_dict):
    details = [part.strip() for part in pkmn_dict[constants.DETAILS].split(",")]
    species = details[0]
    level = "100"
    gender = ""
    shiny = False
    for part in details[1:]:
        if part.startswith("L") and part[1:].isdigit():
            level = part[1:]
        elif part in ["M", "F"]:
            gender = part
        elif part == "shiny":
            shiny = True

    name_line = species
    if gender:
        name_line = "{} ({})".format(name_line, gender)
    if pkmn_dict[constants.ITEM]:
        name_line = "{} @ {}".format(name_line, pkmn_dict[constants.ITEM])

    lines = [name_line]
    lines.append(
        "Ability: {}".format(
            ability_display_name(species, pkmn_dict[constants.REQUEST_DICT_ABILITY])
        )
    )
    lines.append("Level: {}".format(level))
    if shiny:
        lines.append("Shiny: Yes")
    if pkmn_dict.get(constants.TERA_TYPE):
        lines.append("Tera Type: {}".format(pkmn_dict[constants.TERA_TYPE]))
    hp, atk, def_, spa, spd, spe = random_battles_evs()
    lines.append(
        "EVs: {} HP / {} Atk / {} Def / {} SpA / {} SpD / {} Spe".format(
            hp, atk, def_, spa, spd, spe
        )
    )
    lines.append("Hardy Nature")
    for move_id in pkmn_dict[constants.MOVES]:
        lines.append("- {}".format(move_display_name(move_id)))

    return "\n".join(lines)


def team_export_from_request_json(request_json):
    return "\n\n".join(
        pokemon_export_from_request_dict(pkmn_dict)
        for pkmn_dict in request_json[constants.SIDE][constants.POKEMON]
    )


def user_observation_from_request_dict(pkmn_dict):
    """
    Returns (species, set_key) for a pokemon dict from the request JSON,
    with set_key in the same format as the randombattle sets file
    """
    details = [part.strip() for part in pkmn_dict[constants.DETAILS].split(",")]
    species = normalize_name(details[0])
    level = 100
    for part in details[1:]:
        if part.startswith("L") and part[1:].isdigit():
            level = int(part[1:])

    set_key = make_user_set_key(
        level=level,
        item=pkmn_dict.get(constants.ITEM),
        ability=pkmn_dict.get(constants.REQUEST_DICT_ABILITY),
        moves=pkmn_dict.get(constants.MOVES, []),
        tera_type=pkmn_dict.get(constants.TERA_TYPE),
    )
    return species, set_key


def record_user_team_observations(request_json, pokemon_battle_type):
    observations = [
        user_observation_from_request_dict(pkmn_dict)
        for pkmn_dict in request_json[constants.SIDE][constants.POKEMON]
    ]
    record_user_set_observations(pokemon_battle_type, observations)


def dump_team_from_request_json(request_json, pokemon_battle_type, battle_tag):
    team_export = team_export_from_request_json(request_json)

    # include the username so two local bots in the same battle
    # do not overwrite each other's dump
    file_name = "{}_{}".format(battle_tag, normalize_name(FoulPlayConfig.username))

    dump_dir = os.path.join(FoulPlayConfig.dump_team_dir, pokemon_battle_type)
    os.makedirs(dump_dir, exist_ok=True)
    dump_path = os.path.join(dump_dir, "{}.txt".format(file_name))
    with open(dump_path, "w") as f:
        f.write(team_export)
    logger.info("Dumped team to {}".format(dump_path))

    sampled_dir = os.path.join(TEAM_DIR, pokemon_battle_type[:4], "sampled")
    os.makedirs(sampled_dir, exist_ok=True)
    with open(os.path.join(sampled_dir, file_name), "w") as f:
        f.write(team_export)

    if "randombattle" in pokemon_battle_type:
        record_user_team_observations(request_json, pokemon_battle_type)
