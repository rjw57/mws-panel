from setuptools import find_packages, setup

setup(
    name='mws',
    packages=find_packages(),
    install_requires=[
        'PyOpenSSL',
        'celery>=3.1.25,<4.0',
        'django-celery',
        'django-reversion',
        'django-stronghold>=0.2.6',
        'django-ucamlookup>=1.1',
        'django-ucamprojectlight>=1.1',
        'django-ucamwebauth>=1.2',
        'django>=1.11,<1.12',
        'ecdsa',
        'mock>=1.0.1',
        'psycopg2',
        'pycrypto>=2.6.1',
        'pyyaml',
        'redis',
        'requests',
        'tox'
    ],
)
