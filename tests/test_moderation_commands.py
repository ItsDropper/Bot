import unittest

import bot


class ModerationCommandRegistrationTests(unittest.TestCase):
    def test_expected_moderation_commands_are_registered(self):
        commands = {command.name: command for command in bot.tree.get_commands()}
        expected = {"warn", "timeout", "kick", "ban", "unban", "cases"}
        self.assertTrue(expected.issubset(commands.keys()))

    def test_moderation_commands_have_permission_checks(self):
        commands = {command.name: command for command in bot.tree.get_commands()}
        for name in ("warn", "timeout", "kick", "ban", "unban", "cases"):
            with self.subTest(command=name):
                self.assertGreater(len(commands[name].checks), 0)
                self.assertTrue(commands[name].guild_only)


if __name__ == "__main__":
    unittest.main()
