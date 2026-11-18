"""Regression tests for application-command registration."""

import unittest

import bot


class CommandRegistrationTests(unittest.TestCase):
    def test_registered_command_names_are_unique(self):
        commands = bot.tree.get_commands()
        names = [command.name for command in commands]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        self.assertEqual(duplicates, [], f"Duplicate application commands: {duplicates}")

    def test_ticket_assignment_has_one_authoritative_registration(self):
        matches = [command for command in bot.tree.get_commands() if command.name == "ticketassign"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].description, "Assign an open support ticket to a support staff member")


if __name__ == "__main__":
    unittest.main()
