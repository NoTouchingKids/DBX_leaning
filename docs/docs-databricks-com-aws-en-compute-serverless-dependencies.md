[Skip to main content](https://docs.databricks.com/aws/en/compute/serverless/dependencies#__docusaurus_skipToContent_fallback)

On this page

Last updated on **Aug 21, 2026**

This page explains how to configure the serverless environment for notebooks and job tasks. For notebooks, use the **Environment** side pane to select a base environment, install dependencies, configure memory, and apply usage policies. For job tasks, configure the environment when you create or edit a task.

To expand the **Environment** side pane, click the ![environment](<Base64-Image-Removed>) button to the right of the notebook.

![Serverless environment pane](https://docs.databricks.com/aws/en/assets/images/serverless-notebook-environment-514e8603770a5f8ccff486c45acfb274.png)

## Select a base environment [​](https://docs.databricks.com/aws/en/compute/serverless/dependencies\#-select-a-base-environment "Direct link to -select-a-base-environment")

A base environment determines the pre-installed libraries and environment version available for your serverless notebook. The **Base environment** selector in the **Environment** side pane is where you choose your environment. To see details on each environment version, see [Serverless environment versions](https://docs.databricks.com/aws/en/release-notes/serverless/#environment-version). Databricks recommends using the latest version to get the most up-to-date notebook features.

The **Base environment** selector includes the following options:

- **Standard**: The default serverless base environment with Databricks-provided libraries.
- **ML**: A base environment with the Python and system packages from Databricks Runtime for Machine Learning pre-installed. Use this environment to migrate classic Databricks Runtime for Machine Learning workloads to serverless compute. See [ML base environment](https://docs.databricks.com/aws/en/release-notes/serverless/environment-version/five#ml-environment).
- **AI**: An AI-optimized base environment with pre-installed machine learning (ML) libraries. This option appears only when an accelerator (GPU) is selected. See [AI environment](https://docs.databricks.com/aws/en/release-notes/serverless/environment-version/five-gpu#ai-environment).
- **More**: Expands to show additional options:
  - Previous versions of Standard, ML, and AI environments.
  - **Custom**: Specify a custom environment using a YAML file.
- **Workspace environments**: Lists all compatible base environments configured for your workspace by an administrator.

To select a base environment:

1. In the notebook UI, click the **Environment** side pane ![environment](<Base64-Image-Removed>).
2. Under **Base environment**, select an environment from the drop-down menu.
3. Click **Apply**.

## Add dependencies to the notebook [​](https://docs.databricks.com/aws/en/compute/serverless/dependencies\#-add-dependencies-to-the-notebook "Direct link to -add-dependencies-to-the-notebook")

Because serverless does not support compute policies or init scripts, you must install custom dependencies using the **Environment** side pane. You can install dependencies individually or use a shareable base environment to install multiple dependencies.

Databricks caches your notebook's virtual environment, so dependencies don't reinstall every time you reopen a notebook or resume after inactivity. Job tasks that share the same dependency set also benefit from this cache within a run.

To individually install a dependency:

1. In the notebook UI, click the **Environment** side pane ![environment](<Base64-Image-Removed>).

2. In the **Dependencies** section, enter the path of the dependency in the field then click **+Add dependency**. You can specify a dependency in any format that is valid in a [requirements.txt](https://pip.pypa.io/en/stable/reference/requirements-file-format/) file. Python wheel files or Python projects (for example, the directory containing a `pyproject.toml` or a `setup.py`) can be located in workspace files or Unity Catalog volumes.
   - If using a workspace file, the path should be absolute and start with `/Workspace/`.
   - If using a file in a Unity Catalog volume, the path should be in the following format: `/Volumes/<catalog>/<schema>/<volume>/<path>.whl`.
3. Click **Apply** to install the dependencies and restart the Python process.


important

Do not install PySpark or any library that installs PySpark as a dependency on your serverless notebooks. Doing so will stop your session and result in an error. If this occurs, remove the library and [reset your environment](https://docs.databricks.com/aws/en/compute/serverless/dependencies#reset).

To view installed dependencies, click the **Installed** tab in the **Environments** side pane. Open the pip installation logs for the notebook environment by clicking **pip logs** at the bottom of the pane.

note

Workspace admins can configure private or authenticated package repositories as the default pip source for serverless notebooks and jobs. This lets users install packages from internal repositories without specifying `index-url` or `extra-index-url`. See [Configure default package repositories](https://docs.databricks.com/aws/en/admin/workspace-settings/default-package-repositories).

note

Serverless compute does not guarantee a specific CPU architecture. A notebook or job can run on either `aarch64` or `x86_64`, and the architecture can change between runs. Because a dependency might install on a different architecture than the one it was built for, your dependencies must support every architecture the compute might use.

If a dependency is a Python wheel with native (C) extensions, its prebuilt binary is architecture-specific. A wheel built for one architecture fails to install on another, returning an error. To avoid this, use pure-Python wheels (tagged `py3-none-any`) where possible, or include both `aarch64` and `x86_64` wheel variants and constrain each to its architecture with a [`platform_machine` environment marker](https://packaging.python.org/en/latest/specifications/dependency-specifiers/#environment-markers).

### Create a custom environment specification [​](https://docs.databricks.com/aws/en/compute/serverless/dependencies\#create-a-custom-environment-specification "Direct link to create-a-custom-environment-specification")

You can create and reuse custom environment specifications.

1. In a serverless notebook, select a base environment and install any dependencies you want.
2. Click the kebab menu button ![Kebab menu icon.](<Base64-Image-Removed>) at the bottom of the environment pane then click **Export environment**.
3. Save the specification as a Workspace file or in a Unity Catalog volume. Make sure you have permission to write to the destination, or the export fails with a `Forbidden` error.

To use your custom environment specification in a notebook, select **Custom** from the **Base environment** drop-down menu, then use the folder ![Folder icon.](<Base64-Image-Removed>) to select your YAML file.

### Create common tools to share across your workspace [​](https://docs.databricks.com/aws/en/compute/serverless/dependencies\#create-common-tools-to-share-across-your-workspace "Direct link to Create common tools to share across your workspace")

This example stores a utility in a workspace file and installs it as a serverless notebook dependency:

1. Create a folder with the following structure. Make sure other users have read access to this path:



Shell





```shell
helper_utils/
├── helpers/
│   └── __init__.py   # your common functions live here
├── pyproject.toml
```

2. Populate `pyproject.toml` like this:



Python





```python
[project]
name = "common_utils"
version = "0.1.0"
```

3. Add a function to the `init.py` file. For example:



Python





```python
def greet(name: str) -> str:
    return f"Hello, {name}!"
```

4. In the notebook UI, click the **Environment** side pane ![Environment icon.](<Base64-Image-Removed>).

5. In the **Dependencies** section, click **Add Dependency** then enter the path of your util file. For example: `/Workspace/helper_utils`.

6. Click **Apply**.


You can now use the function in your notebook:

Python

```python
from helpers import greet

print(greet("world"))
```

This outputs as:

Text

```text
Hello, world!
```

## Use AI Runtime (serverless GPU) [​](https://docs.databricks.com/aws/en/compute/serverless/dependencies\#use-ai-runtime-serverless-gpu "Direct link to use-ai-runtime-serverless-gpu")

Preview

This feature is in [Public Preview](https://docs.databricks.com/aws/en/release-notes/release-types).

Follow these steps to configure AI Runtime, powered by serverless GPU compute, on your Databricks notebook:

1. From a notebook, click the compute drop-down menu at the top and select **Serverless GPU**.
2. Click the ![Environment icon.](<Base64-Image-Removed>) to open the **Environment** side pane.
3. Select **A10** or **H100** from the **Accelerator** field.
4. Under **Base environment**, select **Standard** for the default environment or **AI** for the AI-optimized environment with pre-installed machine learning (ML) libraries.
5. Click **Apply** and then **Confirm** that you want to apply AI Runtime to your notebook environment.

For more details, see [AI Runtime](https://docs.databricks.com/aws/en/machine-learning/ai-runtime/).

## Use high memory serverless compute [​](https://docs.databricks.com/aws/en/compute/serverless/dependencies\#use-high-memory-serverless-compute "Direct link to use-high-memory-serverless-compute")

Preview

This feature is in [Public Preview](https://docs.databricks.com/aws/en/release-notes/release-types).

If you run into out-of-memory errors in your notebook, configure the notebook to use a higher memory size. This memory size setting increases the size of the REPL memory used when running code in the notebook. It doesn't affect the memory size of the Spark session. Serverless usage with high memory has a higher DBU emission rate than standard memory.

The available memory options are:

- **Standard**: 16 GB total memory.
- **High**: 32 GB total memory.

To configure the notebook's memory setting:

1. In the notebook UI, click the **Environment** side pane ![environment](<Base64-Image-Removed>).
2. Under **Memory**, select **High memory**.
3. Click **Apply**.

This memory setting also applies to notebook job tasks that run using the notebook's memory preferences. Updating the memory preference in the notebook affects the next job run.

## Select a serverless usage policy [​](https://docs.databricks.com/aws/en/compute/serverless/dependencies\#select-a-serverless-usage-policy "Direct link to select-a-serverless-usage-policy")

Preview

This feature is in [Public Preview](https://docs.databricks.com/aws/en/release-notes/release-types).

[Serverless usage policies](https://docs.databricks.com/aws/en/admin/usage/budget-policies) allow your organization to apply custom tags on serverless usage for granular billing attribution.

If your workspace uses serverless usage policies, select the policy you want to apply to the notebook. If a user is assigned to only one serverless usage policy, that policy applies by default.

After connecting to serverless compute, select a policy from the **Environment** side pane:

1. In the notebook UI, click the **Environment** side pane ![environment](<Base64-Image-Removed>).
2. Under **Serverless usage policy** select the serverless usage policy you want to apply to your notebook.
3. Click **Apply**.

After applying, all notebook usage picks up the policy's custom tags.

note

If your notebook originates from a Git repository or does not have an assigned [serverless usage policy](https://docs.databricks.com/aws/en/admin/usage/budget-policies), it defaults to your last chosen serverless usage policy when it is next attached to serverless compute.

## Include the environment in source file exports [​](https://docs.databricks.com/aws/en/compute/serverless/dependencies\#include-the-environment-in-source-file-exports "Direct link to include-the-environment-in-source-file-exports")

For Python notebooks, you can toggle **Include in source file exports** in the environment configuration. When enabled, the base environment and dependencies are stored in [PEP 723](https://peps.python.org/pep-0723/) format in source file exports. This helps persist the environment configuration when notebooks are stored in [Git folders](https://docs.databricks.com/aws/en/repos/) or downloaded as [source files](https://docs.databricks.com/aws/en/notebooks/notebook-format#source-control-notebook-formats).

For example, a notebook using **Standard v5** exports its environment configuration as inline metadata at the top of the file:

Python

```python
# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///
print("Hello World!")
```

## Reset the environment dependencies [​](https://docs.databricks.com/aws/en/compute/serverless/dependencies\#reset-the-environment-dependencies "Direct link to reset-the-environment-dependencies")

If your notebook is connected to serverless compute, Databricks automatically caches the content of the notebook's virtual environment. This means you generally don't need to reinstall the Python dependencies specified in the **Environment** side pane when you open an existing notebook, even if it has been disconnected due to inactivity.

Python virtual environment caching also applies to jobs. When a job runs, any task that shares the same set of dependencies as a completed task in the same run finishes faster, because the cache already contains the required dependencies.

note

If you change the implementation of a custom Python package used in a job on serverless, you must also update its version number so that jobs can pick up the latest implementation.

To clear the environment cache and perform a fresh install of the dependencies specified in the **Environment** side pane of a notebook attached to serverless compute, click the arrow next to **Apply** and then click **Reset to defaults**.

If you install packages that break or change the core notebook or Apache Spark environment, remove the offending packages and then reset the environment. Starting a new session does not clear the entire environment cache.

## Configure environment for job tasks [​](https://docs.databricks.com/aws/en/compute/serverless/dependencies\#configure-environment-for-job-tasks "Direct link to configure-environment-for-job-tasks")

Each job task runs in an isolated environment that includes a base environment and any additional libraries you specify. The base environment sets the Python and Scala runtime version and pre-installed libraries. Tasks inherit the default set of installed libraries from the environment version. To see what's included, see the **Installed Python libraries** or **Installed Java and Scala libraries** section of the [environment version](https://docs.databricks.com/aws/en/release-notes/serverless/#environment-version) you're using.

You can supplement the pre-installed libraries with libraries from [workspace files](https://docs.databricks.com/aws/en/files/workspace), Unity Catalog [volumes](https://docs.databricks.com/aws/en/volumes/), or public package repositories. Only dependencies required for the task are installed at runtime.

To pass environment variables to the application code in your tasks, see [Configure environment variables for serverless jobs](https://docs.databricks.com/aws/en/jobs/environment-variables).

Beta

Selecting a managed base environment is in beta. The **Base environment** drop-down in the **Configure environment** dialog enables you to select from Databricks-provided environments (such as Standard and ML) or workspace-configured environments. Without this feature, the dialog shows an **Environment version** drop-down instead. Workspace administrators can enable this feature from the **Previews** page.

![Configure environment dialog showing the Base environment drop-down expanded with Databricks environments and Workspace environments sections](https://docs.databricks.com/aws/en/assets/images/jobs-configure-environment-base-env-d58305d336c641237406b8275a088bf2.png)

### Configure the environment by task type [​](https://docs.databricks.com/aws/en/compute/serverless/dependencies\#configure-the-environment-by-task-type "Direct link to Configure the environment by task type")

How you configure environments in a job depends on the task type:

- Notebook tasks
- Python script and Python wheel tasks
- Dbt tasks
- JAR tasks

Notebook tasks default to **Notebook Environment**, which uses the notebook's own configured base environment and dependencies. You can override this with a job-level environment.

![Environment and Libraries drop-down for a notebook task showing Notebook Environment and Jobs Environment options](https://docs.databricks.com/aws/en/assets/images/jobs-notebook-task-environment-dropdown-5dcaa3da35e5497c20a7e0771380aa50.png)

To configure a job-level environment:

1. In the task configuration, click the **Environment and Libraries** drop-down menu.
2. In **Jobs Environment**, click the pencil icon next to **Default**, or click **\+ Add new jobs environment**.
3. In the **Configure environment** dialog, select from the **Base environment** drop-down menu:

   - **Databricks environments**: Databricks-provided options such as **Standard** and **ML**.
   - **Workspace environments**: Custom environments configured by your workspace administrator. See [Manage workspace base environments](https://docs.databricks.com/aws/en/admin/workspace-settings/base-environment).
   - **More**: Previous versions and **Custom** (specify a YAML file).
4. Under **Dependencies**, add any additional libraries. You can specify a library in any format valid in a [requirements.txt](https://pip.pypa.io/en/stable/reference/requirements-file-format/) file, or use an absolute path to a workspace file or Unity Catalog volume.
5. Click **Confirm**.

note

If your workspace does not have the workspace base environment for jobs preview enabled, the **Configure environment** dialog shows an **Environment version** drop-down instead of **Base environment**.

To configure the environment, select a version, then click **\+ Add library**. You can specify a workspace file path (starting with `/Workspace/`), a Unity Catalog volume path (starting with `/Volumes/`), or a requirements file reference (for example, `-r /Workspace/path/to/requirements.txt`).

Python script and Python wheel tasks require an environment to be configured.

![Environment and Libraries section for a Python wheel task showing the Add dependency link](https://docs.databricks.com/aws/en/assets/images/jobs-python-task-add-dependency-b224a8e500b68a0f4909fc0d465cd489.png)

1. In the task configuration, under **Environment and Libraries**, click **\+ Add dependency**.
2. In the **Configure environment** dialog, select from the **Base environment** drop-down menu:

   - **Databricks environments**: Databricks-provided options such as **Standard** and **ML**.
   - **Workspace environments**: Custom environments configured by your workspace administrator. See [Manage workspace base environments](https://docs.databricks.com/aws/en/admin/workspace-settings/base-environment).
   - **More**: Previous versions and **Custom** (specify a YAML file).
3. Under **Dependencies**, add any additional libraries.
4. Click **Confirm**.

note

If your workspace does not have the workspace base environment for jobs preview enabled, the **Configure environment** dialog shows an **Environment version** drop-down instead of **Base environment**.

To configure the environment, select a version, then click **\+ Add library**. You can specify a workspace file path (starting with `/Workspace/`), a Unity Catalog volume path (starting with `/Volumes/`), or a requirements file reference (for example, `-r /Workspace/path/to/requirements.txt`).

DBT tasks use a job-level environment for library configuration.

![Environment and Libraries drop-down for a dbt task showing Jobs Environment options](https://docs.databricks.com/aws/en/assets/images/jobs-dbt-task-environment-dropdown-19d6d2f979e30cac84f165a30393e2dd.png)

To configure a job-level environment:

1. In the task configuration, click the **Environment and Libraries** drop-down menu.
2. In **Jobs Environment**, click the pencil icon next to an existing environment, or click **\+ Add new jobs environment**.
3. In the **Configure environment** dialog, select from the **Base environment** drop-down menu:

   - **Databricks environments**: Databricks-provided options such as **Standard** and **ML**.
   - **Workspace environments**: Custom environments configured by your workspace administrator. See [Manage workspace base environments](https://docs.databricks.com/aws/en/admin/workspace-settings/base-environment).
   - **More**: Previous versions and **Custom** (specify a YAML file).
4. Under **Dependencies**, add any additional libraries. You can specify a library in any format valid in a [requirements.txt](https://pip.pypa.io/en/stable/reference/requirements-file-format/) file, or use an absolute path to a workspace file or Unity Catalog volume.
5. Click **Confirm**.

note

If your workspace does not have the workspace base environment for jobs preview enabled, the **Configure environment** dialog shows an **Environment version** drop-down instead of **Base environment**.

To configure the environment, select a version, then click **\+ Add library**. You can specify a workspace file path (starting with `/Workspace/`), a Unity Catalog volume path (starting with `/Volumes/`), or a requirements file reference (for example, `-r /Workspace/path/to/requirements.txt`).

Workspace base environments are not supported for JAR tasks. To configure the environment for a JAR task:

![Environment and Libraries section for a JAR task showing the Add JAR dependency link](https://docs.databricks.com/aws/en/assets/images/jobs-jar-task-add-dependency-3fb481d073e49d10535158e1f5e59b8c.png)

1. In the task configuration, under **Environment and Libraries**, click **\+ Add JAR dependency**.
2. In the **Configure environment** dialog:

   - Optionally, enter a path to a YAML file in the **Base environment** field.
   - Select an environment version from the **Environment version** drop-down menu.
   - Under **JAR Dependencies**, add the paths to your JAR files.
3. Click **Confirm**.

To create a custom YAML-based base environment, see [Create a custom environment specification](https://docs.databricks.com/aws/en/compute/serverless/dependencies#create-a-custom-environment-specification).

### Environment and compute compatibility [​](https://docs.databricks.com/aws/en/compute/serverless/dependencies\#environment-and-compute-compatibility "Direct link to Environment and compute compatibility")

The base environment you select must be compatible with the task's compute type. For example, an environment built for GPU compute is not compatible with CPU compute. In the jobs UI, incompatible environments are unavailable in the base environment drop-down menu.

When you configure a notebook task, the compute type (CPU or GPU) and base environment can each come from either the job settings or the notebook settings.

- If you set a hardware accelerator (GPU) at the job level, you must also select a base environment at the job level. You cannot use the notebook's environment with a job-level accelerator.
- If you have job tasks that reference a notebook, and you update the referenced notebook's compute type (for example, from CPU to GPU), existing tasks might become incompatible with their configured environment. Review your job's environment settings after changing the notebook's compute configuration.
- For API users: if you set the base environment at the job level but the notebook defines the compute type, Databricks validates compatibility at runtime, not at job creation time. If the configuration is incompatible, the run fails with an error.

On this page

- [Select a base environment](https://docs.databricks.com/aws/en/compute/serverless/dependencies#-select-a-base-environment)
- [Add dependencies to the notebook](https://docs.databricks.com/aws/en/compute/serverless/dependencies#-add-dependencies-to-the-notebook)
  - [Create a custom environment specification](https://docs.databricks.com/aws/en/compute/serverless/dependencies#create-a-custom-environment-specification)
  - [Create common tools to share across your workspace](https://docs.databricks.com/aws/en/compute/serverless/dependencies#create-common-tools-to-share-across-your-workspace)
- [Use AI Runtime (serverless GPU)](https://docs.databricks.com/aws/en/compute/serverless/dependencies#use-ai-runtime-serverless-gpu)
- [Use high memory serverless compute](https://docs.databricks.com/aws/en/compute/serverless/dependencies#use-high-memory-serverless-compute)
- [Select a serverless usage policy](https://docs.databricks.com/aws/en/compute/serverless/dependencies#select-a-serverless-usage-policy)
- [Include the environment in source file exports](https://docs.databricks.com/aws/en/compute/serverless/dependencies#include-the-environment-in-source-file-exports)
- [Reset the environment dependencies](https://docs.databricks.com/aws/en/compute/serverless/dependencies#reset-the-environment-dependencies)
- [Configure environment for job tasks](https://docs.databricks.com/aws/en/compute/serverless/dependencies#configure-environment-for-job-tasks)
  - [Configure the environment by task type](https://docs.databricks.com/aws/en/compute/serverless/dependencies#configure-the-environment-by-task-type)
  - [Environment and compute compatibility](https://docs.databricks.com/aws/en/compute/serverless/dependencies#environment-and-compute-compatibility)

Was this page helpful?

YesNo

Send feedback

Ask Genie

Open Genie

|     |     |
| --- | --- |
|  |  |