import os

from setuptools import find_packages, setup

with open('README.rst') as readme_file:
    readme = readme_file.read()


def prerelease_local_scheme(version):
    """
    Return local scheme version unless building on master in CircleCI.

    This function returns the local scheme version number
    (e.g. 0.0.0.dev<N>+g<HASH>) unless building on CircleCI for a
    pre-release in which case it ignores the hash and produces a
    PEP440 compliant pre-release version number (e.g. 0.0.0.dev<N>).
    """
    from setuptools_scm.version import get_local_node_and_date

    if os.getenv('CIRCLE_BRANCH') in ('master', ):
        return ''
    else:
        return get_local_node_and_date(version)


setup(
    name='wsi_deid',
    use_scm_version={'local_scheme': prerelease_local_scheme},
    setup_requires=['setuptools-scm'],
    author='Kitware, Inc.',
    author_email='kitware@kitware.com',
    classifiers=[
        'Development Status :: 4 - Beta',
        'License :: OSI Approved :: Apache Software License',
        'Natural Language :: English',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
    ],
    description='Digital Slide Archive Whole-Slide Image DeIdentification plugin',
    install_requires=[
        'easyocr!=1.4.1',
        'girder-homepage',
        'histomicsui',
        'large_image[tiff,openslide,memcached,openjpeg,converter]',
        'openpyxl',
        'pandas',
        'paramiko',
        'python-magic',
        'pyvips!=2.1.16',
        'xlrd',
    ],
    license='Apache Software License 2.0',
    long_description=readme,
    long_description_content_type='text/x-rst',
    include_package_data=True,
    keywords='girder-plugin, wsi_deid',
    packages=find_packages(exclude=['test', 'test.*']),
    url='https://github.com/DigitalSlideArchive',
    zip_safe=False,
    python_requires='>=3.6',
    entry_points={
        'girder.plugin': [
            'wsi_deid = wsi_deid:GirderPlugin',
        ],
    },
)
