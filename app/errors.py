from pydantic import ValidationError


class APIError(Exception):
    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class ModelResponseError(Exception):
    pass


class JobLeaseLost(Exception):
    pass


def validate_input(model, value):
    try:
        return model.model_validate(value)
    except ValidationError as error:
        details = [
            {"field": ".".join(str(part) for part in item["loc"]), "message": item["msg"]}
            for item in error.errors(include_input=False, include_url=False)
        ]
        raise APIError(400, "invalid_input", "The request contains invalid fields.", details) from None
