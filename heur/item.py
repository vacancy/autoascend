import contextlib
import functools
import re
from functools import partial

import nle.nethack as nh
import numpy as np
from nle.nethack import actions as A

import objects as O
import utils
from exceptions import AgentPanic
from glyph import WEA
from strategy import Strategy


class Item:
    UNKNOWN = 0
    CURSED = 1
    UNCURSED = 2
    BLESSED = 3

    def __init__(self, objs, glyph=None, count=1, status=UNKNOWN, modifier=None, equipped=False, at_ready=False, text=None):
        assert isinstance(objs, list) and len(objs) >= 1
        assert glyph is None or nh.glyph_is_object(glyph)

        self.objs = objs
        self.glyph = glyph
        self.count = count
        self.status = status
        self.modifier = modifier
        self.equipped = equipped
        self.at_ready = at_ready
        self.text = text

        self.category = ord(nh.objclass(O.objects.index(self.objs[0])).oc_class)
        assert glyph is None or ord(nh.objclass(nh.glyph_to_obj(glyph)).oc_class) == self.category

    def is_ambiguous(self):
        return len(self.objs) == 1, self.objs

    def object(self):
        assert self.is_ambiguous()
        return self.objs[0]

    ######## WEAPON

    def is_weapon(self):
        return self.category == nh.WEAPON_CLASS

    def get_dps(self, large_monster):
        assert self.is_weapon()

        weapon = self.object()
        dmg = WEA.expected_damage(weapon.damage_large if large_monster else weapon.damage_small)

        # TODO: take into account things from:
        # https://github.com/facebookresearch/nle/blob/master/src/weapon.c : hitval
        # https://github.com/facebookresearch/nle/blob/master/src/weapon.c : dmgval
        # https://github.com/facebookresearch/nle/blob/master/src/uhitm.c : find_roll_to_hit

        if self.modifier is not None and self.modifier > 0:
            dmg += self.modifier

        to_hit = 1
        to_hit += 6 # compensation, TODO: abon, etc
        to_hit += weapon.hitbon
        if self.modifier is not None:
            to_hit += self.modifier

        return np.array([to_hit > i for i in range(1, 21)]).astype(np.float32).mean().item() * dmg

    def is_launcher(self):
        if not self.is_weapon():
            return False

        return self.object().name in ['bow', 'elven bow', 'orcish bow', 'yumi', 'crossbow', 'sling']

    def is_fired_projectile(self, launcher=None):
        if not self.is_weapon():
            return False

        arrows = ['arrow', 'elven arrow', 'orcish arrow', 'silver arrow', 'ya']

        if launcher is None:
            return self.object().name in (arrows + ['crossbow bolt']) # TODO: sling ammo
        else:
            launcher_name = launcher.object().name
            if launcher_name == 'crossbow':
                return self.object().name == 'crossbow bolt'
            elif launcher_name == 'sling':
                # TODO: sling ammo
                return False
            else:  # any bow
                assert launcher_name in ['bow', 'elven bow', 'orcish bow', 'yumi'], launcher_name
                return self.object().name in arrows

    def is_thrown_projectile(self):
        if not self.is_weapon():
            return False

        # TODO: boomerang
        # TODO: aklys, Mjollnir
        return self.object().name in \
               ['orcish dagger', 'dagger silver', 'athame dagger', 'elven dagger',
                'worm tooth', 'knife', 'stiletto', 'scalpel', 'crysknife',
                'dart', 'shuriken']

    def __str__(self):
        #if self.text is not None:
        #    return self.text
        return (f'{self.count}_'
                f'{self.status if self.status is not None else ""}_'
                f'{self.modifier if self.modifier is not None else ""}_'
                f'{",".join(list(map(lambda x: x.name, self.objs)))}'
                )

    def __repr__(self):
        return str(self)


class ItemManager:
    def __init__(self, agent):
        self.agent = agent

    def get_item_from_text(self, text, category=None, glyph=None):
        # TODO: pass glyph if not on hallu
        return Item(*self.parse_text(text, category, None))

    def possible_objects_from_glyph(self, glyph):
        # TODO: take into account identified objects
        assert nh.glyph_is_object(glyph)
        return O.possibilities_from_glyph(glyph)

    @staticmethod
    @functools.lru_cache(1024 * 256)
    def parse_text(text, category=None, glyph=None):
        assert glyph is None or nh.glyph_is_normal_object(glyph)

        if category is None and glyph is not None:
            category = ord(nh.objclass(nh.glyph_to_obj(glyph)).oc_class)
        assert glyph is None or category is None or category == ord(nh.objclass(nh.glyph_to_obj(glyph)).oc_class)

        assert category not in [nh.BALL_CLASS, nh.RANDOM_CLASS]

        matches = re.findall(
            r'^(a|an|\d+)'
            r'( empty)?'
            r'( (cursed|uncursed|blessed))?'
            r'( (very |thoroughly )?(rustproof|poisoned|corroded|rusty|burnt|rotted|partly eaten|partly used))*'
            r'( ([+-]\d+))? '
            r'([a-zA-z0-9-! ]+)'
            r'( \(([0-9]+:[0-9]+|no charge)\))?'
            r'( \(([a-zA-Z0-9; ]+)\))?'
            r'( \((for sale|unpaid), (\d+) ?[a-zA-Z- ]+\))?'
            r'$',
            text)
        assert len(matches) <= 1, text
        assert len(matches), text

        count, _, effects1, status, effects2, _, _, _, modifier, name, _, uses, _, info, _, shop_status, shop_price = matches[0]
        # TODO: effects, uses

        if info in {'weapon in paw', 'weapon in hand', 'weapon in paws', 'weapon in hands', 'being worn',
                    'being worn; slippery', 'wielded'}:
            equipped = True
            at_ready = False
        elif info in {'at the ready', 'in quiver', 'in quiver pouch', 'lit'}:
            equipped = False
            at_ready = True
        elif info in {'', 'alternate weapon; not wielded'}:
            equipped = False
            at_ready = False
        else:
            assert 0, info

        count = int({'a': 1, 'an': 1}.get(count, count))
        status = {'': Item.UNKNOWN, 'cursed': Item.CURSED, 'uncursed': Item.UNCURSED, 'blessed': Item.BLESSED}[status]
        modifier = None if not modifier else {'+': 1, '-': -1}[modifier[0]] * int(modifier[1:])

        if name == 'wakizashi':
            name = 'short sword'
        elif name == 'ninja-to':
            name = 'broadsword'
        elif name == 'nunchaku':
            name = 'flail'
        elif name == 'shito':
            name = 'knife'
        elif name == 'naginata':
            name = 'glaive'
        elif name == 'gunyoki':
            name = 'food ration'
        elif name == 'osaku':
            name = 'lock pick'
        elif name == 'tanko':
            name = 'plate mail'
        elif name == 'pair of yugake':
            name = 'pair of leather gloves'
        elif name == 'kabuto':
            name = 'helmet'
        elif name in ['potion of holy water', 'potions of holy water']:
            name = 'potion of water'
            status = Item.BLESSED
        elif name in ['potion of unholy water', 'potions of unholy water']:
            name = 'potion of water'
            status = Item.CURSED
        elif name in ['flint stone', 'flint stones']:
            name = 'flint'
        elif name in ['unlabeled scroll', 'unlabeled scrolls', 'blank paper']:
            name = 'scroll of blank paper'
        elif name == 'eucalyptus leaves':
            name = 'eucalyptus leaf'
        elif name == 'pair of lenses':
            name = 'lenses'
        elif name.startswith('small glob'):
            name = name[len('small '):]


        # TODO: pass to Item class instance
        if name.startswith('tin of ') or name.startswith('tins of '):
            name = 'tin'
        elif name.endswith(' corpse') or name.endswith(' corpses') or ' corpse named ' in name:
            name = 'corpse'
        elif name.startswith('statue of ') or name.startswith('statues of '):
            name = 'statue'
        elif name.startswith('figurine of ') or name.startswith('figurines of '):
            name = 'figurine'
        elif name.startswith('paperback book named ') or name.startswith('paperback books named ') or name in ['novel', 'paperback']:
            name = 'spellbook of novel'

        if ' named ' in name:
            # TODO: many of these are artifacts
            name = name[:name.index(' named ')]


        # object identified (look on names)
        obj_ids = set()
        prefixes = [
            ('scroll of ', nh.SCROLL_CLASS),
            ('scrolls of ', nh.SCROLL_CLASS),
            ('spellbook of ', nh.SPBOOK_CLASS),
            ('spellbooks of ', nh.SPBOOK_CLASS),
            ('ring of ', nh.RING_CLASS),
            ('rings of ', nh.RING_CLASS),
            ('wand of ', nh.WAND_CLASS),
            ('wands of ', nh.WAND_CLASS),
            ('amulet of ', nh.AMULET_CLASS),
            ('amulets of ', nh.AMULET_CLASS),
            ('potion of ', nh.POTION_CLASS),
            ('potions of ', nh.POTION_CLASS),
            ('', nh.GEM_CLASS),
            ('', nh.ARMOR_CLASS),
            ('pair of ', nh.ARMOR_CLASS),
            ('', nh.WEAPON_CLASS),
            ('', nh.TOOL_CLASS),
            ('', nh.FOOD_CLASS),
            ('', nh.COIN_CLASS),
            ('', nh.ROCK_CLASS),
        ]
        suffixes = [
            ('s', nh.GEM_CLASS),
            ('s', nh.WEAPON_CLASS),
            ('s', nh.TOOL_CLASS),
            ('s', nh.FOOD_CLASS),
            ('s', nh.COIN_CLASS),
        ]
        for i in range(nh.NUM_OBJECTS):
            for pref, c in prefixes:
                if ord(nh.objclass(i).oc_class) == c:
                    obj_name = nh.objdescr.from_idx(i).oc_name
                    if obj_name and name == pref + obj_name:
                        obj_ids.add(i)

            for suf, c in suffixes:
                if ord(nh.objclass(i).oc_class) == c:
                    obj_name = nh.objdescr.from_idx(i).oc_name
                    if obj_name and (name == obj_name + suf or \
                                     (c == nh.FOOD_CLASS and \
                                      name == obj_name.split()[0] + suf + ' ' + ' '.join(obj_name.split()[1:]))):
                        obj_ids.add(i)


        # object unidentified (look on descriptions)
        appearance_ids = set()
        prefixes = [
            ('scroll labeled ', nh.SCROLL_CLASS),
            ('scrolls labeled ', nh.SCROLL_CLASS),
            ('', nh.ARMOR_CLASS),
            ('pair of ', nh.ARMOR_CLASS),
            ('', nh.WEAPON_CLASS),
            ('', nh.TOOL_CLASS),
            ('', nh.FOOD_CLASS),
        ]
        suffixes = [
            (' amulet', nh.AMULET_CLASS),
            (' amulets', nh.AMULET_CLASS),
            (' gem', nh.GEM_CLASS),
            (' gems', nh.GEM_CLASS),
            (' stone', nh.GEM_CLASS),
            (' stones', nh.GEM_CLASS),
            (' potion', nh.POTION_CLASS),
            (' potions', nh.POTION_CLASS),
            (' spellbook', nh.SPBOOK_CLASS),
            (' spellbooks', nh.SPBOOK_CLASS),
            (' ring', nh.RING_CLASS),
            (' rings', nh.RING_CLASS),
            (' wand', nh.WAND_CLASS),
            (' wands', nh.WAND_CLASS),
            ('s', nh.ARMOR_CLASS),
            ('s', nh.WEAPON_CLASS),
            ('s', nh.TOOL_CLASS),
            ('s', nh.FOOD_CLASS),
        ]

        for i in range(nh.NUM_OBJECTS):
            for pref, c in prefixes:
                if ord(nh.objclass(i).oc_class) == c:
                    obj_descr = nh.objdescr.from_idx(i).oc_descr
                    if obj_descr and name == pref + obj_descr:
                        appearance_ids.add(i)

            for suf, c in suffixes:
                if ord(nh.objclass(i).oc_class) == c:
                    obj_descr = nh.objdescr.from_idx(i).oc_descr
                    if obj_descr and name == obj_descr + suf:
                        appearance_ids.add(i)

        appearance_ids = list(appearance_ids)
        assert len(appearance_ids) == 0 or len({ord(nh.objclass(i).oc_class) for i in appearance_ids}), (text, name)

        assert (len(obj_ids) > 0) ^ (len(appearance_ids) > 0), (name, obj_ids, appearance_ids)
        if obj_ids:
            assert len(obj_ids) == 1, text
            obj_id = list(obj_ids)[0]
            objs = [O.objects[obj_id]]
            if glyph is not None:
                assert objs[0] in O.possibilities_from_glyph(glyph), (objs[0], O.possibilities_from_glyph(glyph))
        else:
            obj_id = list(appearance_ids)[0]
            if glyph is not None:
                assert glyph in list(map(lambda i: i + nh.GLYPH_OBJ_OFF, appearance_ids))
            else:
                if len(appearance_ids) == 1:
                    glyph = obj_id + nh.GLYPH_OBJ_OFF
            objs = O.possibilities_from_glyph(obj_id + nh.GLYPH_OBJ_OFF)
            assert all(map(lambda i: O.possibilities_from_glyph(i + nh.GLYPH_OBJ_OFF) == objs, appearance_ids)), text

        assert category is None or category == ord(nh.objclass(obj_id).oc_class), (text, category, ord(nh.objclass(obj_id).oc_class))
        return objs, glyph, count, status, modifier, equipped, at_ready, text


class Inventory:
    def __init__(self, agent):
        self.agent = agent
        self.item_manager = ItemManager(self)
        self.items = []
        self.letters = []

        self._previous_inv_strs = None
        self._previous_blstats = None
        self._stop_updating = False
        self.items_below_me = None
        self.letters_below_me = None
        self._interesting_item_glyphs = []
        self._interesting_items = []

    def on_panic(self):
        self.items_below_me = None
        self.letters_below_me = None
        self._previous_blstats = None

    def update(self):
        if not (self._previous_inv_strs is not None and (self.agent.last_observation['inv_strs'] == self._previous_inv_strs).all()):
            self.items = []
            self.letters = []
            for item_name, category, glyph, letter in zip(
                    self.agent.last_observation['inv_strs'],
                    self.agent.last_observation['inv_oclasses'],
                    self.agent.last_observation['inv_glyphs'],
                    self.agent.last_observation['inv_letters']):
                item_name = bytes(item_name).decode().strip('\0')
                letter = chr(letter)
                if not item_name:
                    continue
                item = self.item_manager.get_item_from_text(item_name, category=category, glyph=glyph)

                self.items.append(item)
                self.letters.append(letter)

            self._previous_inv_strs = self.agent.last_observation['inv_strs']


        if self._stop_updating:
            return

        if self._previous_blstats is None or \
                (self._previous_blstats.y, self._previous_blstats.x, \
                 self._previous_blstats.level_number, self._previous_blstats.dungeon_number) != \
                (self.agent.blstats.y, self.agent.blstats.x, \
                 self.agent.blstats.level_number, self.agent.blstats.dungeon_number):

            assume_appropriate_message = self._previous_blstats is not None

            self._previous_blstats = self.agent.blstats
            self.items_below_me = None
            self.letters_below_me = None

            try:
                self._stop_updating = True
                self.get_items_below_me(assume_appropriate_message=assume_appropriate_message)
            finally:
                self._stop_updating = False

        assert self.items_below_me is not None and self.letters_below_me is not None

    def get_letter(self, item):
        assert item in self.items, (item, self.items)
        return self.letters[self.items.index(item)]

    @contextlib.contextmanager
    def panic_if_items_below_me_change(self):
        old_items_below_me = self.items_below_me
        old_letters_below_me = self.letters_below_me

        def f(self):
            if (
                [(l, i.text) for i, l in zip(old_items_below_me, old_letters_below_me)] !=
                [(l, i.text) for i, l in zip(self.items_below_me, self.letters_below_me)]
            ):
                raise AgentPanic('items below me changed')

        fun = partial(f, self)

        self.agent.on_update.append(fun)

        try:
            yield
        finally:
            assert fun in self.agent.on_update
            self.agent.on_update.pop(self.agent.on_update.index(fun))

    ####### ACTIONS

    def wield(self, item):
        if item is None: # fists
            letter = '-'
        else:
            letter = self.get_letter(item)

        if item is not None and item.equipped:
            return True

        with self.agent.atom_operation():
            self.agent.step(A.Command.WIELD)
            if "Don't be ridiculous" in self.agent.message:
                return False
            assert 'What do you want to wield' in self.agent.message, self.agent.message
            self.agent.type_text(letter)
            if 'You cannot wield a two-handed sword while wearing a shield.' in self.agent.message or \
                    ' welded to your hand' in self.agent.message:
                # TODO: handle it better
                return False
            assert re.search(r'(You secure the tether\.  )?(^[a-zA-z] - |welds?( itself| themselves| ) to|You are already wielding that|'
                             r'You are already empty handed)', \
                             self.agent.message), self.agent.message

        return True

    def get_items_below_me(self, assume_appropriate_message=False):
        with self.agent.panic_if_position_changes():
            with self.agent.atom_operation():
                if not assume_appropriate_message:
                    self.agent.step(A.Command.LOOK)

                if 'Things that are here:' not in self.agent.popup and not re.search('There are (several|many) objects here\.', self.agent.message):
                    if 'You see no objects here.' in self.agent.message:
                        items = []
                        letters = []
                    elif 'You see here ' in self.agent.message:
                        item_str = self.agent.message[self.agent.message.index('You see here ') + len('You see here '):]
                        item_str = item_str[:item_str.index('.')]
                        items = [self.item_manager.get_item_from_text(item_str)]
                        letters = [None]
                    else:
                        items = []
                        letters = []
                else:
                    self.agent.step(A.Command.PICKUP)
                    if 'Pick up what?' not in self.agent.popup:
                        if 'You cannot reach the bottom of the pit.' in self.agent.message or \
                                'There is nothing here to pick up.' in self.agent.message or \
                                ' solidly fixed to the floor.' in self.agent.message or \
                                'You read:' in self.agent.message or \
                                "You don't see anything in here to pick up." in self.agent.message:
                            items = []
                            letters = []
                        else:
                            assert 0, (self.agent.message, self.agent.popup)
                    else:
                        lines = self.agent.popup[self.agent.popup.index('Pick up what?') + 1:]
                        name_to_category = {
                            'Amulets': nh.AMULET_CLASS,
                            'Armor': nh.ARMOR_CLASS,
                            'Comestibles': nh.FOOD_CLASS,
                            'Coins': nh.COIN_CLASS,
                            'Gems/Stones': nh.GEM_CLASS,
                            'Potions': nh.POTION_CLASS,
                            'Rings': nh.RING_CLASS,
                            'Scrolls': nh.SCROLL_CLASS,
                            'Spellbooks': nh.SPBOOK_CLASS,
                            'Tools': nh.TOOL_CLASS,
                            'Weapons': nh.WEAPON_CLASS,
                            'Wands': nh.WAND_CLASS,
                            'Boulders/Statues': nh.ROCK_CLASS,
                        }
                        category = None
                        items = []
                        letters = []
                        for line in lines:
                            if line in name_to_category:
                                category = name_to_category[line]
                                continue
                            assert line[1:4] == ' - ', line
                            letter, line = line[0], line[4:]
                            letters.append(letter)
                            items.append(self.item_manager.get_item_from_text(line, category))

                self.items_below_me = items
                self.letters_below_me = letters
                return items

    def pickup(self, items):
        if isinstance(items, Item):
            items = [items]
        assert len(items) > 0
        assert all(map(lambda item: item in self.items_below_me, items))

        letters = [self.letters_below_me[self.items_below_me.index(item)] for item in items]
        assert len(set(letters)) == len(letters), 'TODO: not implemented'

        with self.panic_if_items_below_me_change():
            self.get_items_below_me()

        with self.agent.atom_operation():
            if len(self.items_below_me) == 1:
                self.agent.step(A.Command.PICKUP)
            else:
                self.agent.step(A.Command.PICKUP, iter(letters + [A.MiscAction.MORE]))
        return True


    ######## STRATEGIES helpers

    def update_interesting_items(self):
        # TODO
        if self.agent.character.role == self.agent.character.RANGER:
            self._interesting_item_glyphs = np.array(list(range(1, 6))) + nh.GLYPH_OBJ_OFF
        else:
            self._interesting_item_glyphs = []

        self._interesting_items = set()
        for glyph in self._interesting_item_glyphs:
            for object in self.item_manager.possible_objects_from_glyph(glyph):
                self._interesting_items.add(object)

        #if self.character.role == Character.MONK:
        #    yield False

        #current_weapon = self.get_best_weapon()
        #if current_weapon is None:
        #    current_weapon_dps = -2
        #    current_weapon = 'fists'
        #else:
        #    current_weapon_dps = current_weapon.get_dps(large_monster=False)

        #current_weapon_dps *= 1.3  # take only relatively better items than yours

        #if only_below_me:
        #    best_item = None
        #    best_dps = current_weapon_dps
        #    for item in self.inventory.items_below_me:
        #        if item.is_weapon():
        #            dps = item.get_dps(large_monster=False)
        #            if dps > best_dps:
        #                best_dps = dps
        #                best_item = item
        #    if best_item is not None:
        #        yield True
        #        self.inventory.pickup(best_item)
        #        return
        #    yield False


        #mask = ((self.last_observation['specials'] & nh.MG_OBJPILE) == 0) & ~self.current_level().shop & (
        #        self.bfs() != -1) & utils.isin(self.glyphs, G.WEAPONS)
        #nonzero_y, nonzero_x = mask.nonzero()

        #best_item = None
        #best_item_dps = None
        #for y, x in zip(nonzero_y, nonzero_x):
        #    glyph = self.glyphs[y, x]
        #    dps = Item([O.possibilities_from_glyph(self.glyphs[y, x])[0]]).get_dps(large_monster=False)
        #    if dps > current_weapon_dps:
        #        best_item_dps = dps
        #        best_item = (y, x)

        #if best_item is None:
        #    yield False

        #yield True

        #with self.env.debug_log(f'going for {Item([O.possibilities_from_glyph(self.glyphs[y, x])[0]])}'):
        #    target_y, target_x = best_item
        #    self.go_to(target_y, target_x, debug_tiles_args=dict(color=(255, 0, 255), is_path=True))
        #    if self.current_level().shop[target_y, target_x]:
        #        return
        #    if self.inventory.items_below_me:
        #        self.inventory.pickup(self.inventory.items_below_me)

        #    self.wield_best_weapon()
        #    if self.blstats.carrying_capacity != 0:
        #        print(self.blstats.carrying_capacity)


    ######## LOW-LEVEL STRATEGIES

    def gather_items(self):
        return self.pickup_items_below_me().before(
               self.go_to_item().preempt(self.agent, [
                   self.pickup_items_below_me()
               ])).repeat()

    @utils.debug_log('inventory.go_to_item')
    @Strategy.wrap
    def go_to_item(self):
        mask = ((self.agent.last_observation['specials'] & nh.MG_OBJPILE) > 0) & \
               ~self.agent.current_level().checked_item_pile
        mask |= utils.isin(self.agent.glyphs, self._interesting_item_glyphs)

        if not mask.any():
            yield False

        dis = self.agent.bfs()
        mask &= dis != -1
        if not mask.any():
            yield False
        yield True

        nonzero_y, nonzero_x = (mask & (dis == dis[mask].min())).nonzero()
        i = self.agent.rng.randint(len(nonzero_y))
        target_y, target_x = nonzero_y[i], nonzero_x[i]

        with self.agent.env.debug_tiles(mask, color=(255, 0, 0, 128)):
            # TODO: search for traps before stepping in
            self.agent.go_to(target_y, target_x, debug_tiles_args=dict(color=(255, 0, 255), is_path=True))

    @utils.debug_log('inventory.pickup_items_below_me')
    @Strategy.wrap
    def pickup_items_below_me(self):
        if len(self.items_below_me) > 1:
            self.agent.current_level().checked_item_pile[self.agent.blstats.y, self.agent.blstats.x] = True

        self.update_interesting_items() # TODO: optimize (do it less frequently)

        to_pickup = []
        for item in self.items_below_me:
            if item.glyph is not None and item.glyph in self._interesting_item_glyphs:
                to_pickup.append(item)
            elif len(set(item.objs).intersection(self._interesting_items)) == len(item.objs):
                to_pickup.append(item)

        yield bool(to_pickup)
        self.pickup(to_pickup)
