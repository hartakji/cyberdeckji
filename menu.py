"""Customise the menu tree here.

build(specs) receives every app/script found in apps/ and custom/ and must
return the root Menu. Start from the default tree and edit it, or build your own."""
from deckfw import Menu, Submenu, Launch, default_menu, custom_menu


def build(specs):
    defaults = {item.label: item for item in default_menu(specs).items}
    scan = next((spec for spec in specs
                 if spec.source == "custom" and spec.name == "Network quick check"), None)

    return Menu("Main", [
        defaults["Apps"],
        Submenu("Custom", custom_menu(specs, [
            Submenu("Network", Menu("Network", [
                Launch(scan),
            ]), icon="icons/network.png"),
        ] if scan is not None else []), icon="icons/custom.png"),
        defaults["System"],
        defaults["Exit"],
    ])
