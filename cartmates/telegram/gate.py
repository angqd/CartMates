from telegram import Message, MessageEntity


def parse_command(message: Message) -> tuple[str, str, str | None] | None:
    """(name, args, target_bot) if the text starts with a /command, else None."""
    entities = message.entities or ()
    if not message.text or not any(
        e.type == MessageEntity.BOT_COMMAND and e.offset == 0 for e in entities
    ):
        return None
    head, _, args = message.text.partition(" ")
    name, _, target = head[1:].partition("@")
    return name.lower(), args.strip(), target or None


def is_login_paste(text: str | None) -> bool:
    """A pasted Swiggy login redirect URL. People paste it as a plain message as often
    as a reply, so it passes the gate on its own; the handler checks a login is pending."""
    return bool(text) and "/callback?" in text and "code=" in text


def is_for_bot(message: Message, bot_id: int, bot_username: str) -> bool:
    command = parse_command(message)
    if command:
        # "/list@otherbot" is for someone else, even if it also replies to us.
        target = command[2]
        return target is None or target.lower() == bot_username.lower()
    for entity in message.entities or ():
        if entity.type == MessageEntity.MENTION:
            # Compare the entity's own text: a substring search would also match
            # "@cartmates_fan" or an email address.
            if message.parse_entity(entity).lower() == f"@{bot_username.lower()}":
                return True
    if is_login_paste(message.text):
        return True
    reply = message.reply_to_message
    return bool(reply and reply.from_user and reply.from_user.id == bot_id)
