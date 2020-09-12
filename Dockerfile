FROM python:3.8-slim-buster

# Create project directory (workdir)
WORKDIR /app

RUN apt-get update \
 && apt-get install -y libpq-dev gcc

# Add requirements.txt to WORKDIR and install dependencies
COPY requirements.txt .
RUN pip install -r requirements.txt

# Add source code files to WORKDIR
ADD . .

# Application port (optional)
EXPOSE 8000

# Container start command
# It is also possible to override this in devspace.yaml via images.*.cmd
#CMD ["/bin/sh","/app/ENTRYPOINT.sh"]
CMD ["/bin/bash","/app/ENTRYPOINT.sh"]
#CMD ["python",  "manage.py", "run_binancer"]
#CMD ["python",  "manage.py", "run_telegramer"]
