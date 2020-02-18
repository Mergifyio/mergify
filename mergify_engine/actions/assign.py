import voluptuous

from mergify_engine import actions


class AssignAction(actions.Action):
    validator = {voluptuous.Required("users", default=[]): [str]}

    def run(self, pull, sources, missing_conditions):
        pull.g_pull.as_issue().add_to_assignees(*self.config["users"])
