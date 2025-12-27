import atexit
import logging
import shutil
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional

from Code.app_vars import AppConfig
from Code.loc import Localization as loc
from Code.package.dataclasses import ModUnit
from Code.xml_object import XMLBuilder, XMLComment, XMLElement

from .condition_manager import process_condition
from .parts_manager import PartsManager

logger = logging.getLogger(__name__)


class ModManager:
    active_mods: List[ModUnit] = []
    inactive_mods: List[ModUnit] = []

    @staticmethod
    def init():
        ModManager.load_mods()
        ModManager.load_cslua_config()
        atexit.register(ModManager._on_exit)

    @staticmethod
    def load_mods():
        game_path = AppConfig.get_game_path()
        if not game_path:
            return

        ModManager.active_mods.clear()
        ModManager.inactive_mods.clear()

        active_mod_configs = ModManager._get_active_mod_configs(game_path / "config_player.xml")

        all_mods = {}

        def process_mod_folder(path: Path):
            if not path or not path.is_dir() or path.name.startswith('.'):
                return
            if not (path / "filelist.xml").exists():
                logger.debug(f"Пропускаем папку без filelist.xml: {path}")
                return
            
            try:
                mod = ModUnit.build(path)
                if mod:
                    all_mods[mod.id] = mod
            except Exception as e:
                logger.error(f"Ошибка при обработке папки мода {path.name}: {e}")

        scan_paths = [game_path / "LocalMods"]
        workshop_content_path_str = AppConfig.get("workshop_sync_path")
        if workshop_content_path_str:
            scan_paths.append(Path(workshop_content_path_str))
        legacy_workshop_path_str = AppConfig.get("steam_mod_dir")
        if legacy_workshop_path_str:
            scan_paths.append(Path(legacy_workshop_path_str))

        for path in scan_paths:
            if path and path.exists():
                for mod_folder in path.iterdir():
                    process_mod_folder(mod_folder)

        for mod_id, mod in all_mods.items():
            if mod_id in active_mod_configs:
                mod.load_order = active_mod_configs[mod_id]
                ModManager.active_mods.append(mod)
            else:
                ModManager.inactive_mods.append(mod)

        ModManager.active_mods.sort(key=lambda m: m.load_order)

    @staticmethod
    def _get_active_mod_configs(path_to_config_player: Path) -> dict:
        """Вспомогательный метод: читает config_player.xml и возвращает словарь {mod_id: load_order}."""
        if not path_to_config_player.exists():
            return {}

        xml_obj = XMLBuilder.load(path_to_config_player)
        if not xml_obj:
            return {}

        active_mod_configs = {}

        packages = xml_obj.find_only_elements("package")
        package_paths = [
            (i, package.attributes.get("path"))
            for i, package in enumerate(packages, start=1)
            if package.tag == "package" and package.attributes.get("path")
        ]

        for index, path_str in package_paths:
            try:

                mod_folder_name = Path(path_str).parent.name

                if mod_folder_name.isdigit():
                    active_mod_configs[mod_folder_name] = index
                else:
                    pass # TODO: Improve LocalMod ID detection
            except Exception:
                continue # Игнорируем некорректные пути

        return active_mod_configs

    @staticmethod
    def load_cslua_config():
        game_path = AppConfig.get_game_path()
        if not game_path:
            return

        # LUA
        lua_dep_path = game_path / "Barotrauma.deps.json"
        if lua_dep_path.exists():
            with open(lua_dep_path, "r", encoding="utf-8") as file:
                has_lua = "Luatrauma" in file.read()
                AppConfig.set("has_lua", has_lua)
                logger.debug(f"Lua support enabled: {has_lua}")

        else:
            AppConfig.set("has_lua", False)
            logger.debug("Barotrauma.deps.json not found, disabling Lua support.")

        # CS
        config_path = game_path / "LuaCsSetupConfig.xml"
        if config_path.exists():
            xml_obj = XMLBuilder.load(config_path)
            has_cs = (
                xml_obj.attributes.get("EnableCsScripting", "false").lower() == "true"
                if xml_obj
                else False
            )
            AppConfig.set("has_cs", has_cs)
            logger.debug(f"CS scripting enabled: {has_cs}")

        else:
            AppConfig.set("has_cs", False)
            logger.debug("LuaCsSetupConfig.xml not found, disabling CS scripting.")

    @staticmethod
    def find_mod_by_id(mod_id: str) -> Optional[ModUnit]:
        for mod in ModManager.active_mods + ModManager.inactive_mods:
            if mod.id == mod_id:
                return mod

        return None

    @staticmethod
    def get_mod_by_id(mod_id: str) -> Optional[ModUnit]:
        for mod in ModManager.active_mods + ModManager.inactive_mods:
            if mod.id == mod_id:
                return mod

        return None

    @staticmethod
    def activate_mod(mod_id: str) -> bool:
        mod = ModManager.get_mod_by_id(mod_id)
        if mod and mod in ModManager.inactive_mods:
            ModManager.inactive_mods.remove(mod)
            ModManager.active_mods.append(mod)
            return True

        return False

    @staticmethod
    def deactivate_mod(mod_id: str) -> bool:
        mod = ModManager.get_mod_by_id(mod_id)
        if mod and mod in ModManager.active_mods:
            ModManager.active_mods.remove(mod)
            ModManager.inactive_mods.append(mod)
            return True

        return False
        
    @staticmethod
    def activate_all_mods():
        ModManager.active_mods.extend(ModManager.inactive_mods)
        ModManager.inactive_mods.clear()
        logger.info("all mods active")

    @staticmethod
    def swap_active_mods(mod_id1: str, mod_id2: str) -> None:
        mod1 = ModManager.get_mod_by_id(mod_id1)
        mod2 = ModManager.get_mod_by_id(mod_id2)
        if (
            mod1
            and mod2
            and mod1 in ModManager.active_mods
            and mod2 in ModManager.active_mods
        ):
            idx1, idx2 = (
                ModManager.active_mods.index(mod1),
                ModManager.active_mods.index(mod2),
            )
            ModManager.active_mods[idx1], ModManager.active_mods[idx2] = (
                ModManager.active_mods[idx2],
                ModManager.active_mods[idx1],
            )

    @staticmethod
    def swap_inactive_mods(mod_id1: str, mod_id2: str) -> None:
        mod1 = ModManager.get_mod_by_id(mod_id1)
        mod2 = ModManager.get_mod_by_id(mod_id2)
        if (
            mod1
            and mod2
            and mod1 in ModManager.inactive_mods
            and mod2 in ModManager.inactive_mods
        ):
            idx1, idx2 = (
                ModManager.inactive_mods.index(mod1),
                ModManager.inactive_mods.index(mod2),
            )
            ModManager.inactive_mods[idx1], ModManager.inactive_mods[idx2] = (
                ModManager.inactive_mods[idx2],
                ModManager.inactive_mods[idx1],
            )

    @staticmethod
    def move_active_mod_to_end(mod_id: str) -> None:
        mod = ModManager.get_mod_by_id(mod_id)
        if mod and mod in ModManager.active_mods:
            ModManager.active_mods.remove(mod)
            ModManager.active_mods.append(mod)

    @staticmethod
    def move_inactive_mod_to_end(mod_id: str) -> None:
        mod = ModManager.get_mod_by_id(mod_id)
        if mod and mod in ModManager.inactive_mods:
            ModManager.inactive_mods.remove(mod)
            ModManager.inactive_mods.append(mod)

    @staticmethod
    def save_mods() -> None:
        game_path = AppConfig.get("barotrauma_dir", None)
        if not game_path:
            logger.error("Game path not set!")
            return

        game_path = Path(game_path)
        if not game_path.exists():
            logger.error(f"Game path does not exist!\n|Path: {game_path}")
            return

        user_config_path = game_path / "config_player.xml"
        if not user_config_path.exists():
            logger.error(
                f"config_player.xml does not exist!\n|Path: {user_config_path}"
            )
            return

        user_config_path = user_config_path.resolve()
        if not user_config_path.is_file():
            logger.error(f"Resolved path is not a valid file: {user_config_path}")
            return

        xml_obj = XMLBuilder.load(user_config_path)
        if xml_obj is None:
            logger.error(f"Invalid config_player.xml\n|Path: {user_config_path}")
            return

        regularpackages = next(
            (item for item in xml_obj.find_only_elements("regularpackages")), None
        )
        if regularpackages is None:
            logger.error("No 'regularpackages' element found in config_player.xml.")
            return

        regularpackages.childrens.clear()

        active_mod_id = set([mod.id for mod in ModManager.active_mods])
        for mod in ModManager.active_mods:
            if mod.has_toggle_content:
                PartsManager.do_chenges(mod, active_mod_id)

            mod_path = mod.get_str_path()
            regularpackages.add_child(XMLComment(mod.name))
            regularpackages.add_child(
                XMLElement("package", {"path": f"{mod_path}/filelist.xml"})
            )

        del active_mod_id

        XMLBuilder.save(xml_obj, user_config_path)

    @staticmethod
    def _on_exit():
        try:
            if not (ModManager.active_mods + ModManager.inactive_mods):
                return

            game_path = AppConfig.get("barotrauma_dir", None)
            if not game_path:
                logger.error("Game path not set!")
                return

            game_path = Path(game_path)
            if not game_path.exists():
                logger.error(f"Game path does not exist!\n|Path: {game_path}")
                return

            user_config_path = game_path / "config_player.xml"
            if not user_config_path.exists():
                logger.error(
                    f"config_player.xml does not exist!\n|Path: {user_config_path}"
                )
                return

            user_config_path = user_config_path.resolve()
            if not user_config_path.is_file():
                logger.error(f"Resolved path is not a valid file: {user_config_path}")
                return

            xml_obj = XMLBuilder.load(user_config_path)
            if xml_obj is None:
                logger.error(f"Invalid config_player.xml\n|Path: {user_config_path}")
                return

            regularpackages = next(
                (item for item in xml_obj.find_only_elements("regularpackages")), None
            )
            if regularpackages is None:
                logger.error("No 'regularpackages' element found in config_player.xml.")
                return

            regularpackages.childrens.clear()

            for mod in ModManager.active_mods:
                try:
                    mod_path = mod.get_str_path()
                    regularpackages.add_child(XMLComment(mod.name))
                    regularpackages.add_child(
                        XMLElement("package", {"path": f"{mod_path}/filelist.xml"})
                    )

                    if mod.has_toggle_content:
                        try:
                            PartsManager.rollback_changes_no_thread(mod)
                        except Exception as e:
                            logger.error(
                                f"Error rolling back changes for mod {mod.name}: {e}"
                            )
                except Exception as e:
                    logger.error(f"Error processing mod {mod.name}: {e}")
                    continue

            XMLBuilder.save(xml_obj, user_config_path)

        except Exception as e:
            logger.error(f"Error during exit processing: {e}")

    @staticmethod
    def process_errors():
        active_mods_ids = {mod.id for mod in ModManager.active_mods}
        bind_id = {}
        for mod in ModManager.active_mods:
            mod.update_meta_errors()
            for dep in mod.metadata.dependencies:
                if dep.type == "conflict":
                    if dep.id in active_mods_ids:
                        level = dep.attributes.get("level", "error")
                        if level == "warning":
                            mod.metadata.warnings.append(
                                dep.attributes.get("message", "base-conflict")
                            )
                        else:
                            mod.metadata.errors.append(
                                dep.attributes.get("message", "base-conflict")
                            )

                elif dep.condition:
                    if process_condition(
                        dep.condition, active_mods_ids=active_mods_ids
                    ):
                        if dep.id not in active_mods_ids:
                            mod.metadata.errors.append(
                                loc.get_string(
                                    "mod-unfind-mod",
                                    mod_name=dep.name,
                                    mod_id=dep.steam_id,
                                )
                            )
                else:
                    if dep.id not in active_mods_ids:
                        mod.metadata.errors.append(
                            loc.get_string(
                                "mod-unfind-mod",
                                mod_name=dep.name,
                                mod_id=dep.steam_id,
                            )
                        )

        for mod in ModManager.active_mods:
            for over_id in mod.override_id:
                if over_id not in bind_id:
                    bind_id[over_id] = (mod.name, mod.id)

        for mod in ModManager.active_mods:
            for over_id in mod.override_id:
                if over_id in bind_id:
                    mod.metadata.warnings.append(
                        loc.get_string(
                            "mod-override-id",
                            mod_name=bind_id[over_id][0],
                            mod_id=bind_id[over_id][1],
                            key_id=over_id,
                        )
                    )

    @staticmethod
    def sort():
        mods = ModManager.active_mods
        id_to_mod = {mod.id: mod for mod in mods}
        id_to_name = {mod.id: mod.name for mod in mods}
        active_mod_ids = set(id_to_mod.keys())
        ban_ids = set()

        dependency_graph = defaultdict(list)
        in_degree = defaultdict(int)
        added_ids = {}

        for mod in mods:
            for dep in mod.metadata.dependencies:
                if dep.condition and not process_condition(
                    dep.condition, active_mod_ids=active_mod_ids
                ):
                    continue

                dep_id = dep.id

                if dep.type == "conflict":
                    if dep_id in id_to_mod:
                        ban_ids.add(dep_id)
                        logger.error(
                            f"Conflict detected between '{mod.name}' and '{id_to_name.get(dep_id, dep_id)}'."
                        )
                    continue

                if dep_id not in id_to_mod:
                    if not ModManager.activate_mod(dep_id):
                        logger.warning(
                            f"Dependency '{dep_id}' specified in mod '{mod.name}' not found among active mods."
                        )
                        continue
                    else:
                        if dep_id in ban_ids:
                            logger.error(
                                f"Conflict detected between '{mod.name}' and '{id_to_name.get(dep_id, dep_id)}'."
                            )
                            continue

                        on_mod = ModManager.get_mod_by_id(dep_id)
                        id_to_mod[on_mod.id] = on_mod  # type: ignore
                        id_to_name[on_mod.id] = on_mod.name  # type: ignore
                        active_mod_ids.add(on_mod.id)  # type: ignore

                if dep.type == "patch":
                    dependency_graph[mod.id].append(dep_id)
                    in_degree[dep_id] += 1

                elif dep.type == "requirement":
                    dependency_graph[dep_id].append(mod.id)
                    in_degree[mod.id] += 1

                elif dep.type == "requiredAnyOrder":
                    pass

        for mod in mods:
            for add_id in mod.add_id:
                if add_id in added_ids:
                    logger.warning(
                        f"Conflict: add_id '{add_id}' already added by '{id_to_name[added_ids[add_id]]}' but '{mod.name}' try add one more time"
                    )
                else:
                    added_ids[add_id] = mod.id

        for mod in mods:
            if not mod.get_bool_settigs("IgnoreOverrideCheck"):
                for override_id in mod.override_id:
                    if override_id in added_ids:
                        adder_mod_id = added_ids[override_id]
                        if adder_mod_id != mod.id:
                            dependency_graph[adder_mod_id].append(mod.id)
                            in_degree[mod.id] += 1

        queue = deque(
            sorted(
                [mod_id for mod_id in id_to_mod if in_degree[mod_id] == 0],
                key=lambda id: id_to_name[id],
            )
        )

        sorted_mods: List[ModUnit] = []
        while queue:
            current_id = queue.popleft()
            current_mod = id_to_mod[current_id]
            sorted_mods.append(current_mod)

            for neighbor_id in sorted(
                dependency_graph[current_id], key=lambda id_: id_to_name[id_]
            ):
                in_degree[neighbor_id] -= 1
                if in_degree[neighbor_id] == 0:
                    queue.append(neighbor_id)

        if len(sorted_mods) != len(mods):
            unresolved_mods = set(id_to_mod.keys()) - set(mod.id for mod in sorted_mods)
            unresolved_names = [id_to_name[mod_id] for mod_id in unresolved_mods]
            logger.error(
                f"Unresolved dependencies or cycles detected for mods: {', '.join(unresolved_names)}"
            )
            return

        for i, mod in enumerate(sorted_mods, 1):
            mod.load_order = i

        ModManager.active_mods = sorted_mods
