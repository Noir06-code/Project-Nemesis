
import json
import os

class TempManager:
    def __init__(self, file_name="temp/temp.json"):
        self.file = file_name
        self.data = self.read()

    def add(self, text):
        self.data = self.read()

        count = max(map(int, self.data.keys()), default=0) + 1
        self.data[str(count)] = text

        self.delete()
        self.save()

    def read(self):
        if not os.path.exists(self.file):
            return {}

        try:
            with open(self.file, "r") as file:
                return json.load(file)
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self):
        os.makedirs(os.path.dirname(self.file) or ".", exist_ok=True)

        with open(self.file, "w") as file:
            json.dump(self.data, file, indent=4)

    def update(self, count, text):
        self.data = self.read()

        if str(count) in self.data:
            self.data[str(count)] = text
            self.save()
            return True

        return False

    def delete(self):
        while len(self.data) >5:
            oldest = min(self.data.keys(), key=int)
            del self.data[oldest]


temp  = TempManager()
temp.add("h")