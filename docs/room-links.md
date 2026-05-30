# Room Links

Room links are configured server-side in YAML.

Each linked room can define categories, relay behavior, and whether full topic content is allowed.

New Discourse topics are delivered to Matrix rooms as regular text messages, with the topic title on the first line and the topic body below it.

When `full_content` is disabled, Dischat keeps the title and shortens only the body excerpt.
