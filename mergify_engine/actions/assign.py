import voluptuous

from mergify_engine import actions


class AssignAction(actions.Action):
    validator = {voluptuous.Required("users", default=[]): [str]}

    silent_report = True

    def run(self, pull, sources, missing_conditions):
        pull.g_pull.as_issue().add_to_assignees(*self.config["users"])
        return ("success", "Assignees added", ", ".join(self.config["users"]))
