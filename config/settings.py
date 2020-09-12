"""
Django settings for binancer project.

Generated by 'django-admin startproject' using Django 3.1.1.

For more information on this file, see
https://docs.djangoproject.com/en/3.1/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/3.1/ref/settings/
"""
import logging
from logging import config
import os
from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/3.1/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = 'i5w-f#$(+=7s)$l^1@oc5@y$8-tm_n4@5+%qdmbehz=b!3q@ya'

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

ALLOWED_HOSTS = []

# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'cryptoman',
    'binancer',
    'telegramer'
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'binancer.wsgi.application'

# Database
# https://docs.djangoproject.com/en/3.1/ref/settings/#databases

# DATABASES = {
#     'default': {
#         'ENGINE': 'django.db.backends.sqlite3',
#         'NAME': BASE_DIR / 'db.sqlite3',
#     }
# }
DATABASES = {
    "default": {
        "ENGINE": os.getenv("DB_ENGINE"),
        "NAME": os.getenv("DB_NAME1"),
        "USER": os.getenv("DB_USER"),
        "PASSWORD": os.getenv("DB_PASSWORD"),
        "HOST": os.getenv("DB_HOST"),
        "PORT": os.getenv("DB_PORT"),
    }
}

# Password validation
# https://docs.djangoproject.com/en/3.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
# https://docs.djangoproject.com/en/3.1/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_L10N = True

USE_TZ = False

TIME_ZONE = 'Europe/Berlin'

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/3.1/howto/static-files/

STATIC_URL = '/static/'

format_simple = '%(log_color)s%(filename)s@%(lineno)5s (%(asctime)s%(levelname).1s)> %(message)s'
datefmt_simple = "%H:%M"

DEFAULT_LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'simple': {
            'format': format_simple,
            'datefmt': '%H:%M'
        },
        'colored': {
            '()': 'colorlog.ColoredFormatter',
            'format': format_simple,
            'reset': False,
            'datefmt': datefmt_simple,
            'log_colors': {
                'DEBUG': 'cyan',
                'INFO': 'green',
                'WARNING': 'yellow',
                'ERROR': 'red',
                'CRITICAL': 'red',
            },
        }
    },
    'handlers': {
        'console': {
            'level': "DEBUG",
            'class': 'logging.StreamHandler',
            'formatter': 'colored',
        },
        # 'db_log': {
        #     'level': 'DEBUG',
        #     'class': 'django_db_logger.db_log_handler.DatabaseLogHandler'
        # },
        # 'tg_log': {
        #     'level': 'DEBUG',
        #     'class': 'binancer.logger_config.TGHandler'
        # },
        # 'ws_log': {
        #     'level': 'DEBUG',
        #     'class': 'binancer.logger_config.RedisHandler'
        # },
        # 'trade_log': {
        #     'level': 'DEBUG',
        #     'class': 'binancer.logger_config.TradeLogHandler'
        # }
    },
    'loggers': {
        '': {
            'level': "DEBUG",
            'handlers': ['console'],
            'propagate': False,
        },
        # 'db': {
        #     'handlers': ['db_log', 'tg_log', 'ws_log', 'trade_log', 'console'],
        #     'level': 'DEBUG',
        #     'propagate': True,
        # },
    },
}
logging.config.dictConfig(DEFAULT_LOGGING)
AUTH_USER_MODEL = 'cryptoman.MyUser'

TG_HOOK_TOKEN = 'TGBOT'